"""The slow half of an installation's defence: catching drift the request path deliberately skips.

`extension_scope` refuses a credential whose installation is suspended or gone, and it does that
from a cached snapshot so authentication stays cheap. What it deliberately does not do is re-check
whether the organization still reaches every cluster in the grant. That join on every
authenticated request is the worse trade, so the drift is left to this sweep and the window is
named by `EXTENSION_RECONCILE_INTERVAL_SECONDS`.

note: the organization's bindings, not the installing partner's own scope. Install time enforces
the intersection of both, and this reconciles only the organization half. That is deliberate on
one ground and forced on another. The ground: an installation is granted to an organization, and
the organization's binding is what entitles it afterwards, so a partner later losing personal
access does not retract an entitlement the organization still holds. The force:
`get_current_allowed_clusters` is a ContextVar with no value in a background task, so the
install-time helper cannot be reused here, and reimplementing a nearly-identical notion of partner
scope would risk narrowing grants that are in fact correct. Partner-scope drift is therefore not
reconciled, and this says so rather than leaving the docstring claiming a guarantee.

important: without this the stored grant is trusted indefinitely, which is the exact failure
`extension_scope`'s own docstring names. On a platform where clusters are rented, an installation
whose organization lost a cluster would keep reading it after the hardware changed hands.

Two passes, both idempotent, so every worker can run them without a leader lock:

1. Narrow a grant to the clusters the organization is still bound to. An installation left with
   nothing it can reach is suspended rather than left holding an empty grant, because an empty
   grant reads like a configuration mistake while a suspension says what happened.
2. Revoke a credential whose installation is gone. Vault is written before the row is deleted on
   purpose (`extension_teardown`), so a teardown interrupted between the two leaves a key that
   still authenticates with nothing left to say whose it is. This is what reclaims it.

note: both passes revoke even a credential that owns OrcaHub organizations, because a credential
no installation claims, or one whose organization reaches nothing, must stop. The record then
names the organizations the revocation freezes (services/integration_key_ownership.py), so an
administrator can transfer them.
"""

import asyncio
import logging
from contextlib import AsyncExitStack
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import HTTPException
from sqlalchemy import select

from app.core.api_key_expiry import now_utc, parse_expiry

from app.models.db_models import OrganizationCluster
from app.models.extension_models import ExtensionInstallation
from app.services.activity_logger import get_activity_logger
from app.services.extension_credential_guard import live_credentials, lock_installation_id
from app.services.extension_credentials import revoke_install_credential
from app.services.extension_install import _rewrite_key_grant
from app.services.extension_scope import evict_install_cache
from app.services.integration_key_ownership import owned_organizations, serialize_key_ownership

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

_task: Optional[asyncio.Task] = None

# important: how old a credential no installation claims must be before it is revoked. An install or a
# rotation writes the key to Vault before its row commits, so a key minted moments ago can be missing
# from the rows read below without being an orphan. A real orphan is refused at authentication anyway
# (extension_scope.assert_install_is_live); this revocation is the backstop, so waiting costs nothing.
ORPHAN_MIN_AGE = timedelta(minutes=10)


async def _bound_clusters(db, organization_id: int) -> Set[str]:
    rows = await db.execute(
        select(OrganizationCluster.cluster_id).where(
            OrganizationCluster.organization_id == organization_id
        )
    )
    return {row[0] for row in rows.all() if row[0]}


FROZEN_WARNING = (
    "This credential owned OrcaHub organizations. They stop syncing until an administrator "
    "transfers them to another key."
)


async def _owned_by(db, key_id: str) -> Optional[List[str]]:
    """Slugs of the active OrcaHub organizations a credential owns, or None when unreadable.

    note: a failed read is rolled back here, so the session stays usable for the rest of the pass.
    """
    try:
        return [org.slug for org in await owned_organizations(db, key_id)]
    except Exception as exc:  # noqa: BLE001 - the record is best effort, the revocation is not
        logger.warning("Reconcile could not read which organizations %s owns: %s", key_id, type(exc).__name__)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


def _frozen_fields(owned: Optional[List[str]]) -> Dict[str, Any]:
    if owned is None:
        return {"owned_organization_count": None}
    fields: Dict[str, Any] = {"owned_organization_slugs": owned[:20], "owned_organization_count": len(owned)}
    if owned:
        fields["warning"] = FROZEN_WARNING
    return fields


async def _narrow_drifted_scope(db, vault_client) -> int:
    """Trim every grant to what its organization still reaches. Returns rows changed.

    important: suspended rows are narrowed too. Filtering on active looked like a saving and was a
    hole: `resume` only flips the status back, so suspending an installation, losing a cluster and
    resuming it returned a grant naming a cluster the organization no longer has, until the next
    tick. Narrowing a suspended row costs nothing and closes the window instead of shortening it,
    which is better than narrowing on resume because it does not depend on the second path
    remembering to.

    important: the CREDENTIAL is rewritten, not just the row, and that is the whole point. This
    pass shipped writing `granted_scope` and nothing else, which did nothing at all: the request
    path authorizes clusters from the Vault key's `cluster_permissions`, never from the row, and
    the one request-time reader of `granted_scope` is called without a cluster to check. So an
    organization that lost one of several clusters got a narrowed row, an audit record saying so,
    and an add-on that kept full access to the cluster it lost. `_rewrite_key_grant` states that
    invariant in its own comment and the operator-driven path already honoured it; only this one
    did not.

    Worse than merely inert, because the two places that DO read `granted_scope` hid the drift:
    webhook deliveries for the dropped cluster stopped and the add-on's self-describe reported the
    shortened list, so an operator saw a narrowing that had not happened.

    Vault first, row second. A failure then leaves the credential WIDER than the row rather than
    the row narrower than the credential, so the row never claims a reach the key does not have.
    Each row is its own transaction with its own guard: a Vault outage or a bad row delays that
    installation to the next tick instead of ending the pass for everyone.
    """
    # important: candidates as plain values, read once. Each change below is made on the row read
    # again under lock_installation_id with the keys' locks held, so a rotation or a rescope in
    # between is seen rather than overwritten (a stale row rewrote the retired key, and the new one
    # kept the dropped cluster for good, since the next tick compared the row with itself). And no
    # ORM object is touched after a rollback, which expires them all: one failing row used to make
    # the next attribute read raise and end the pass, the orphan pass included.
    candidates: List[Tuple[int, int, List[Any]]] = [
        (row.id, row.organization_id, list(row.granted_scope or []))
        for row in (await db.execute(select(ExtensionInstallation))).scalars().all()
    ]

    changed = 0
    bound_by_org: Dict[int, Set[str]] = {}

    for install_id, org_id, seen_scope in candidates:
        if org_id not in bound_by_org:
            bound_by_org[org_id] = await _bound_clusters(db, org_id)
        if all(isinstance(g, dict) and g.get("cluster_id") in bound_by_org[org_id] for g in seen_scope):
            continue

        key_change: Dict[str, Any] = {}
        try:
            async with AsyncExitStack() as held:
                install = await lock_installation_id(db, install_id)
                # note: the bindings read again too, under the row's lock; a narrowing is not undone
                reachable = await _bound_clusters(db, install.organization_id)
                granted: List[Any] = list(install.granted_scope or [])
                kept = [
                    grant
                    for grant in granted
                    if isinstance(grant, dict) and grant.get("cluster_id") in reachable
                ]
                if len(kept) == len(granted):
                    await db.commit()
                    continue
                dropped = sorted(
                    {
                        grant.get("cluster_id")
                        for grant in granted
                        if isinstance(grant, dict) and grant.get("cluster_id") not in reachable
                    }
                )
                keys = live_credentials(install)
                for key_id in keys:
                    await held.enter_async_context(serialize_key_ownership(db, key_id))
                # important: an empty grant is NEVER written. `ck_ext_install_scope_nonempty` refuses
                # it, so the old code's total-loss branch raised IntegrityError on commit, rolled the
                # suspension back with it, and escaped to the loop's blanket handler: one
                # totally-drifted installation aborted the entire scope pass on every tick, forever,
                # taking unrelated partial narrowings with it. The constraint is right - an
                # installation with no reach is a contradiction - so the row keeps its last grant as
                # the record of what was withdrawn and every live credential is revoked instead.
                total_loss = not kept
                if total_loss:
                    for key_id in keys:
                        revoke_install_credential(vault_client, key_id)
                else:
                    install.granted_scope = kept
                    key_change = _rewrite_key_grant(vault_client, install)
                if total_loss and install.status == "active":
                    install.status = "suspended"
                    install.suspend_reason = (
                        "Reconcile: this organization no longer reaches any cluster in the grant"
                    )
                api_key_id = install.api_key_id
                await db.commit()
        except HTTPException as exc:
            await db.rollback()
            if exc.status_code == 404:
                continue  # uninstalled since the candidates were read
            logger.warning(
                "Reconcile could not narrow installation %s, retrying next tick: %s", install_id, exc.detail,
            )
            continue
        except Exception as exc:  # noqa: BLE001 - one row must not end the pass
            await db.rollback()
            logger.warning(
                "Reconcile could not narrow installation %s, retrying next tick: %s",
                install_id, exc,
            )
            continue
        # The row moved without the Vault key moving, so only the install tombstone carries it.
        for key_id in keys:
            evict_install_cache(key_id)
        changed += 1

        frozen: Dict[str, Any] = {}
        if total_loss:
            frozen = _frozen_fields(await _owned_by(db, api_key_id))
            if frozen.get("warning"):
                logger.warning(
                    "Reconcile revoked credential %s of installation %s, which owns OrcaHub organizations",
                    api_key_id, install_id,
                )
        activity_logger.log_activity(
            action="extension.install.scope_narrowed",
            target_type="extension_installation",
            target_id=str(install_id),
            details={
                "dropped_clusters": dropped,
                "remaining": len(kept),
                "suspended": not kept,
                "api_key_id": api_key_id,
                "revoked_api_key_ids": keys if total_loss else [],
                **key_change,
                **frozen,
            },
        )
        logger.info(
            "Reconcile narrowed installation %s: dropped %s, %s remaining",
            install_id,
            ",".join(dropped),
            len(kept),
        )

    return changed


def _old_enough(created_at: Any, cutoff: datetime) -> bool:
    """Minted before the cutoff. A missing or unreadable time counts as old: nothing new lacks one."""
    try:
        minted = parse_expiry(created_at)
    except Exception:  # noqa: BLE001 - an unreadable time must not shield a key from revocation
        return True
    return minted is None or minted <= cutoff


async def _revoke_orphaned_credentials(db, vault_client) -> int:
    """Revoke install credentials no live installation claims. Returns keys revoked."""
    from app.routers.integrations import list_api_keys_from_vault

    # note: taken before the rows are read, so any key minted earlier than it has its row visible below
    cutoff = now_utc() - ORPHAN_MIN_AGE
    live: Set[str] = set()
    rows = await db.execute(
        select(ExtensionInstallation.api_key_id, ExtensionInstallation.previous_api_key_id)
    )
    for current, previous in rows.all():
        if current:
            live.add(current)
        # A rotation's superseded key is still legitimately usable inside its grace window.
        if previous:
            live.add(previous)

    revoked = 0
    # important: off the event loop. The listing is one blocking Vault read per key ever minted,
    # 1286 on the dev deployment, and this pass runs in every worker every interval. Inline, it
    # stalled each worker's loop for the whole read on every tick.
    try:
        keys = await asyncio.to_thread(list_api_keys_from_vault, vault_client)
    except HTTPException as exc:
        # A sealed Vault answers the listing with 503 now rather than an empty list. Revoking
        # nothing is what that empty list did, so the pass waits for the next interval.
        if exc.status_code != 503:
            raise
        logger.warning("Orphan credential pass skipped: the key store is unavailable")
        return 0
    for key in keys or []:
        install_id = key.get("extension_install_id")
        if install_id is None:
            continue
        # note: the credential metadata calls it "id"; there is no "key_id" field, and a
        # wrong name here would make the pass return a confident zero rather than fail.
        key_id = key.get("id")
        if not key_id or key_id in live:
            continue
        if key.get("is_revoked"):
            continue
        if not _old_enough(key.get("created_at"), cutoff):
            continue

        try:
            # note: a call inside a lambda, not the function passed by name, so the ratchet in
            # test_every_write_that_ends_a_key_checks_what_it_owns.py still sees this pass end a key
            await asyncio.to_thread(lambda: revoke_install_credential(vault_client, key_id))
        except Exception as exc:
            # One unreadable key must not abandon the rest of the pass. The next tick retries it.
            logger.warning("Reconcile could not revoke orphan %s: %s", key_id, exc)
            continue
        revoked += 1
        frozen = _frozen_fields(await _owned_by(db, key_id))
        activity_logger.log_activity(
            action="extension.credential.orphan_revoked",
            target_type="extension_installation",
            target_id=str(install_id),
            details={"key_id": key_id, "reason": "no installation claims this credential", **frozen},
        )
        logger.warning(
            "Reconcile revoked orphaned credential %s for installation %s", key_id, install_id
        )

    return revoked


async def reconcile_once(session_factory, vault_factory) -> Dict[str, int]:
    """One full pass. Safe to run concurrently on every worker: both passes are idempotent.

    important: the Vault client is obtained FIRST now, because both passes need it. The scope pass
    used to run without one, which is exactly why its narrowing never reached a credential. A
    sealed Vault therefore skips both passes rather than running a scope pass that would write a
    row the credential does not match - the state this function exists to prevent.
    """
    try:
        vault_client = vault_factory()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Reconcile skipped: the credential store is unavailable: %s", exc)
        return {
            "scope_narrowed": 0, "orphans_revoked": 0,
            "scope_pass_ran": False, "orphan_pass_ran": False,
        }
    if vault_client is None:
        return {
            "scope_narrowed": 0, "orphans_revoked": 0,
            "scope_pass_ran": False, "orphan_pass_ran": False,
        }

    async with session_factory() as db:
        narrowed = await _narrow_drifted_scope(db, vault_client)
        revoked = await _revoke_orphaned_credentials(db, vault_client)

    return {
        "scope_narrowed": narrowed, "orphans_revoked": revoked,
        "scope_pass_ran": True, "orphan_pass_ran": True,
    }


async def reconcile_loop(session_factory, vault_factory, *, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            result = await reconcile_once(session_factory, vault_factory)
            if result["scope_narrowed"] or result["orphans_revoked"]:
                logger.info("Reconcile pass: %s", result)
        except Exception as exc:  # one bad pass must not kill the loop
            logger.warning("Extension reconcile pass failed: %s", exc)


def start_reconcile_sweep(session_factory, vault_factory, *, interval: float) -> None:
    """Idempotent: safe to call more than once per worker."""
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(
            reconcile_loop(session_factory, vault_factory, interval=interval)
        )
        logger.info("Extension reconcile sweep started on a %ss interval", interval)
