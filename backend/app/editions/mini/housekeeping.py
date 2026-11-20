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
PRUNE_EVERY_SECONDS = 24 * 3600

# What the Host Health checks read (they run in threads and must not touch the event loop).
STATE: Dict[str, Any] = {"collected_at": None}
_task: Optional[asyncio.Task] = None
_last_prune = 0.0


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
    global _last_prune
    ms = mini_settings()
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
    try:
        await collect()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Collecting Mini health figures failed: %s", type(exc).__name__)


async def _loop() -> None:
    while True:
        await tick()
        await asyncio.sleep(TICK_SECONDS)


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
