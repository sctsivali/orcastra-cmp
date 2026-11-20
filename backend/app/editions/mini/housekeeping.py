"""Mini's background work: retire certificates a renewal replaced, prune the audit log, keep the
figures Host Health reports. One loop per worker; the database locks make the writes run once.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy import func, or_, select

from app.core.database import async_session_maker
from app.editions.mini import audit, identities
from app.editions.mini.settings import mini_settings
from app.models.mini_identity_models import LocalIdentity, LocalIdentityCertificate

logger = logging.getLogger(__name__)

TICK_SECONDS = 600
# Host Health reads the figures every 30 s and says so; collecting them only with the ten-minute
# maintenance left "No Administrator can sign in" on screen for minutes after an enrollment.
COLLECT_SECONDS = 30
PRUNE_EVERY_SECONDS = 24 * 3600

# What the Host Health checks read (they run in threads and must not touch the event loop).
STATE: Dict[str, Any] = {"collected_at": None}
_task: Optional[asyncio.Task] = None
_last_prune = 0.0
_last_maintenance = 0.0
_names_seeded = False


def _seed_display_names() -> int:
    """Copy each registered cluster's display name from its Vault record into the node metadata
    every label reads, where the deployment has none. 1.0.0-RC1 could not write that file (its
    directory belonged to root), so after an upgrade every cluster showed its id. Never
    overwrites a name already there."""
    from app.core.config import get_settings
    from app.core.node_metadata import get_node_metadata_store
    from app.core.vault_client import get_vault_client
    from app.services.lxd_cert_manager import LXDCertificateManager

    settings = get_settings()
    manager = LXDCertificateManager(vault_client=get_vault_client(settings.vault_addr, settings.vault_token),
                                    clusters_path=settings.vault_clusters_path)
    store = get_node_metadata_store()
    seeded = 0
    registry = manager.read_registry(True)
    if not registry.readable:
        # Sealed or unreachable: a remembered or empty list is not the registry. Try again.
        raise RuntimeError("the cluster registry is not readable yet")
    for cluster_id in registry.cluster_ids:
        if store.get_node_display_name(cluster_id):
            continue
        name = ((manager.get_cluster_info(cluster_id) or {}).get("metadata") or {}).get("display_name")
        if name and name != cluster_id:
            store.set_node_display_name(cluster_id, name)
            seeded += 1
    return seeded


async def collect() -> None:
    ms = mini_settings()
    now = datetime.now(timezone.utc)
    soon = now + timedelta(days=ms.cert_expiry_warning_days)
    async with async_session_maker() as session:
        usable = or_(LocalIdentityCertificate.not_after.is_(None), LocalIdentityCertificate.not_after > now)
        admins = (await session.execute(
            select(func.count(func.distinct(LocalIdentity.id))).select_from(LocalIdentity).join(
                LocalIdentityCertificate, LocalIdentityCertificate.identity_id == LocalIdentity.id
            ).where(LocalIdentity.role == "admin", LocalIdentity.status == "active",
                    LocalIdentityCertificate.status == "active", usable)
        )).scalar_one()
        operators = (await session.execute(
            select(func.count()).select_from(LocalIdentity).where(LocalIdentity.status == "active")
        )).scalar_one()
        expiring = (await session.execute(
            select(func.count(func.distinct(LocalIdentityCertificate.identity_id))).select_from(LocalIdentityCertificate)
            .join(LocalIdentity, LocalIdentity.id == LocalIdentityCertificate.identity_id).where(
                LocalIdentity.status == "active", LocalIdentityCertificate.status == "active",
                LocalIdentityCertificate.not_after > now, LocalIdentityCertificate.not_after <= soon,
            )
        )).scalar_one()
    writer = audit.get_writer()
    STATE.update({
        "collected_at": time.time(), "usable_admins": admins, "active_operators": operators,
        "expiring_operators": expiring, "bootstrap_token_set": bool(ms.bootstrap_admin_token),
        "audit_enabled": ms.audit_db_enabled, "audit_writer_running": writer.running,
        "audit_dropped": writer.dropped, "audit_failed": writer.failed, "audit_last_error": writer.last_error,
        "retention_days": ms.audit_db_retention_days,
    })


async def tick() -> None:
    global _last_maintenance, _names_seeded
    ms = mini_settings()
    if not _names_seeded:
        try:
            seeded = await asyncio.to_thread(_seed_display_names)
            _names_seeded = True
            if seeded:
                logger.info("Restored the display names of %d clusters from their registration", seeded)
        except Exception as exc:  # noqa: BLE001 - Vault sealed or unreachable: try again next tick
            logger.debug("Cluster display names not seeded yet: %s", type(exc).__name__)
    if time.time() - _last_maintenance >= TICK_SECONDS:
        _last_maintenance = time.time()
        await _maintain(ms)
    try:
        await collect()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Collecting Mini health figures failed: %s", type(exc).__name__)


async def _maintain(ms) -> None:
    global _last_prune
    try:
        await identities.retire_overlapping(ms.cert_renew_overlap_hours)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Retiring renewed certificates failed: %s", type(exc).__name__)
    if ms.audit_db_retention_days > 0 and time.time() - _last_prune > PRUNE_EVERY_SECONDS:
        try:
            result = await audit.prune(ms.audit_db_retention_days)
            _last_prune = time.time()
            if result.get("pruned"):
                logger.info("Audit retention removed %s rows through id %s", result["pruned"], result.get("through_id"))
        except Exception as exc:  # noqa: BLE001
            logger.error("Audit retention failed: %s", type(exc).__name__)


async def _loop() -> None:
    while True:
        await tick()
        await asyncio.sleep(COLLECT_SECONDS)


def start() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop(), name="mini-housekeeping")


async def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _task = None
