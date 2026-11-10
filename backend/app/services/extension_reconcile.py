"""Background sweep for installation drift that the request path does not check.

`extension_scope` authenticates from a cached snapshot and does not re-check that the organization
still reaches every granted cluster, so an installation whose organization lost a cluster would
keep reaching it. This sweep checks every `EXTENSION_RECONCILE_INTERVAL_SECONDS`, against the
organization's bindings only: a binding is what entitles an installation after install time, and
`get_current_allowed_clusters` has no value in a background task.

Two idempotent passes, so every worker runs them without a leader lock:

1. Narrow each grant to the clusters its organization is still bound to; an installation left
   with nothing is suspended and its credentials revoked.
2. Revoke credentials no installation claims: a failed teardown delete, or an install or rotation
   whose row never committed. Both passes revoke even a key that owns OrcaHub organizations; the
   audit record names what that freezes so an administrator can transfer them.
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
# from the rows read below without being an orphan. Authentication already refuses a real orphan
# (extension_scope.assert_install_is_live), so waiting costs nothing.
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

    A failed read is rolled back here, so the session stays usable for the rest of the pass.
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

    important: suspended rows are narrowed too. `resume` only flips the status back, so a skipped
    suspended row would return holding a cluster its organization no longer has.

    important: the credential is rewritten, not only the row. The request path authorizes clusters
    from the Vault key's `cluster_permissions`, never from `granted_scope`, so a row-only narrowing
    would leave the add-on its full reach while webhooks and self-describe showed the narrower list.

    Vault first, row second: a failure leaves the row wider than the credential, never narrower, so
    the row never shows a narrowing the key did not get. Each row is its own transaction, so a
    Vault outage or a bad row delays that installation to the next tick instead of ending the pass.
    """
    # important: candidates are plain values, read once. Each change below is made on the row read
    # again under lock_installation_id with the keys' locks held, so a rotation or a rescope in
    # between is seen rather than overwritten. Plain values also survive a rollback, which expires
    # every ORM object, so one failing row cannot make the next attribute read raise.
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
                # The bindings are read again under the row's lock: a narrowing is never undone.
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
                # important: never write an empty grant. `ck_ext_install_scope_nonempty` refuses it,
                # and the IntegrityError would roll back the suspension and fail this row on every
                # tick. On total loss the row keeps its last grant as the record of what was
                # withdrawn, and every live credential is revoked instead.
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
        # The install snapshot cache is separate from the key cache, so evict it for the changed row.
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

    # Taken before the rows are read, so any key minted earlier than it has its row visible below.
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
    # important: off the event loop. The listing is one blocking Vault read per key ever minted, and
    # this pass runs in every worker every interval.
    try:
        keys = await asyncio.to_thread(list_api_keys_from_vault, vault_client)
    except HTTPException as exc:
        # A sealed Vault answers the listing with 503; the pass revokes nothing and waits for the
        # next interval.
        if exc.status_code != 503:
            raise
        logger.warning("Orphan credential pass skipped: the key store is unavailable")
        return 0
    for key in keys or []:
        install_id = key.get("extension_install_id")
        if install_id is None:
            continue
        # The metadata field is "id"; there is no "key_id" field, and a wrong name here would
        # make the pass silently revoke nothing.
        key_id = key.get("id")
        if not key_id or key_id in live:
            continue
        if key.get("is_revoked"):
            continue
        if not _old_enough(key.get("created_at"), cutoff):
            continue

        try:
            # A call inside a lambda, not the function passed by name, so the ratchet in
            # test_every_write_that_ends_a_key_checks_what_it_owns.py sees this pass end a key.
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

    The Vault client is obtained first because both passes need it. Without one both passes are
    skipped, since a scope pass without Vault would write rows the credentials do not match.
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
