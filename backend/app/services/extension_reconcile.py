"""The slow half of an installation's defence: catching drift the request path deliberately skips.

`extension_scope` refuses a credential whose installation is suspended or gone, and it does that
from a cached snapshot so authentication stays cheap. What it deliberately does not do is re-run
the two-table join that decides whether the installing partner still reaches every cluster in the
grant. That join on every authenticated request is the worse trade, so the drift is left to this
sweep and the window is named by `EXTENSION_RECONCILE_INTERVAL_SECONDS`.

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
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional, Set

from sqlalchemy import select

from app.models.db_models import OrganizationCluster
from app.models.extension_models import ExtensionInstallation
from app.services.activity_logger import get_activity_logger
from app.services.extension_credentials import revoke_install_credential
from app.services.extension_scope import evict_install_cache

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

_task: Optional[asyncio.Task] = None


async def _bound_clusters(db, organization_id: int) -> Set[str]:
    rows = await db.execute(
        select(OrganizationCluster.cluster_id).where(
            OrganizationCluster.organization_id == organization_id
        )
    )
    return {row[0] for row in rows.all() if row[0]}


async def _narrow_drifted_scope(db) -> int:
    """Trim every live grant to what its organization still reaches. Returns rows changed."""
    installs = (
        await db.execute(
            select(ExtensionInstallation).where(ExtensionInstallation.status == "active")
        )
    ).scalars().all()

    changed = 0
    bound_by_org: Dict[int, Set[str]] = {}

    for install in installs:
        org_id = install.organization_id
        if org_id not in bound_by_org:
            bound_by_org[org_id] = await _bound_clusters(db, org_id)
        reachable = bound_by_org[org_id]

        granted: List[Any] = list(install.granted_scope or [])
        kept = [
            grant
            for grant in granted
            if isinstance(grant, dict) and grant.get("cluster_id") in reachable
        ]
        if len(kept) == len(granted):
            continue

        dropped = sorted(
            {
                grant.get("cluster_id")
                for grant in granted
                if isinstance(grant, dict) and grant.get("cluster_id") not in reachable
            }
        )
        install.granted_scope = kept
        if not kept:
            install.status = "suspended"
            install.suspend_reason = (
                "Reconcile: this organization no longer reaches any cluster in the grant"
            )
        await db.commit()
        # The row moved without the Vault key moving, so only the install tombstone carries it.
        evict_install_cache(install.api_key_id)
        changed += 1

        activity_logger.log_activity(
            action="extension.install.scope_narrowed",
            target_type="extension_installation",
            target_id=str(install.id),
            details={
                "dropped_clusters": dropped,
                "remaining": len(kept),
                "suspended": not kept,
            },
        )
        logger.info(
            "Reconcile narrowed installation %s: dropped %s, %s remaining",
            install.id,
            ",".join(dropped),
            len(kept),
        )

    return changed


async def _revoke_orphaned_credentials(db, vault_client) -> int:
    """Revoke install credentials no live installation claims. Returns keys revoked."""
    from app.routers.integrations import list_api_keys_from_vault

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
    for key in list_api_keys_from_vault(vault_client) or []:
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

        revoke_install_credential(vault_client, key_id)
        revoked += 1
        activity_logger.log_activity(
            action="extension.credential.orphan_revoked",
            target_type="extension_installation",
            target_id=str(install_id),
            details={"key_id": key_id, "reason": "no installation claims this credential"},
        )
        logger.warning(
            "Reconcile revoked orphaned credential %s for installation %s", key_id, install_id
        )

    return revoked


async def reconcile_once(session_factory, vault_factory) -> Dict[str, int]:
    """One full pass. Safe to run concurrently on every worker: both passes are idempotent."""
    async with session_factory() as db:
        narrowed = await _narrow_drifted_scope(db)
        try:
            vault_client = vault_factory()
        except Exception as exc:
            # A sealed or unreachable Vault must not stop the scope pass, which needs no Vault.
            logger.warning("Reconcile skipped the orphan pass: %s", exc)
            return {"scope_narrowed": narrowed, "orphans_revoked": 0, "orphan_pass_ran": False}

        if vault_client is None:
            return {"scope_narrowed": narrowed, "orphans_revoked": 0, "orphan_pass_ran": False}

        revoked = await _revoke_orphaned_credentials(db, vault_client)

    return {"scope_narrowed": narrowed, "orphans_revoked": revoked, "orphan_pass_ran": True}


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
