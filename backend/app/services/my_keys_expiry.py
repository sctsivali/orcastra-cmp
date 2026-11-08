"""Revoke each My Keys credential on its cluster once the key's expiry passes.

A key's expiry used to be only its certificate's NotAfter. A client can replace its own
certificate (certificate_self_renewal; shown on the Incus lab 2026-11-08 with a hundred-year
replacement), so the date the dashboard showed bound nothing. One worker (Redis lease) revokes the
cluster half when expires_at passes: the identity and its group on LXD, the certificate on Incus
(services/lxd/client_identities.py). The record stays, marked revoked_at, so the owner still sees
the key and its trail. A cluster that is unreachable, or that does not confirm the removal, is
tried again on the next pass. Owner decision 2026-11-08: every engine.

important: revocation is idempotent on both engines (a credential already gone counts as revoked),
so with Redis down and every worker running the loop the worst case is a repeated audit line.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, List, Optional, Set

from sqlalchemy import select

from app.models.my_key_models import MyKeyRecord
from app.services.activity_logger import get_activity_logger
from app.services.collector_leadership import CollectorLease
from app.services.lxd import client_identities
from app.services.lxd.lazy_client import client_for

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

LEASE_KEY = "orcastra:my_keys_expiry:lease"
INTERVAL_SECONDS = 300.0
VAULT_KEYS_PATH = "my_keys"  # routers/my_keys.py writes the records here

_task: Optional[asyncio.Task] = None

_REVOKED, _SETTLED, _LATER = "revoked", "settled", "later"
# Keys this process saw revoked or gone. Expired records stay until their owner deletes them, so
# without this every pass would read each one from Vault again for the life of the worker.
_settled: Set[str] = set()

ExpiredIds = Callable[[Any, datetime], Awaitable[List[str]]]


async def _expired_key_ids(session_maker: Callable[[], Any], now: datetime) -> List[str]:
    async with session_maker() as db:
        rows = await db.execute(
            select(MyKeyRecord.key_id).where(MyKeyRecord.expires_at <= now, MyKeyRecord.deleted_at.is_(None))
        )
        return [row[0] for row in rows.all()]


def _due(record: dict, now: datetime) -> bool:
    try:
        expires = datetime.fromisoformat(str(record.get("expires_at")))
    except (TypeError, ValueError):
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires <= now


async def sweep(
    session_maker: Callable[[], Any],
    *,
    service: Any,
    vault: Any,
    now: Optional[datetime] = None,
    expired_key_ids: ExpiredIds = _expired_key_ids,
) -> List[str]:
    """Revoke every expired key not revoked yet; returns the ids revoked this pass."""
    now = now or datetime.now(timezone.utc)
    revoked: List[str] = []
    for key_id in await expired_key_ids(session_maker, now):
        if key_id in _settled:
            continue
        try:
            outcome = await _revoke_one(key_id, service=service, vault=vault, now=now)
        except Exception:  # noqa: BLE001 - one cluster must not hold up the keys on the others
            logger.warning("Expired key %s: revocation failed, tried again next pass", key_id, exc_info=True)
            continue
        if outcome != _LATER:
            _settled.add(key_id)
        if outcome == _REVOKED:
            revoked.append(key_id)
    if revoked:
        from app.core.cache import get_cache_manager

        get_cache_manager().invalidate_resource_cache("my_keys")
    return revoked


async def _revoke_one(key_id: str, *, service: Any, vault: Any, now: datetime) -> str:
    path = f"secret/data/{VAULT_KEYS_PATH}/{key_id}/metadata"
    record = await asyncio.to_thread(vault.vault_read, path)
    if not record or record.get("revoked_at"):
        return _SETTLED
    if not _due(record, now):
        return _LATER
    # note: get_client is memory-only and answers None on a worker that never connected to this
    # cluster, which the lease holder often has not; client_for connects from Vault.
    client = await client_for(service, record.get("node"), label="my_keys_expiry")
    if client is None:
        return _LATER
    errors = await asyncio.to_thread(client_identities.revoke, client, record)
    _audit(key_id, record, errors)
    if errors:
        logger.warning("Expired key %s not revoked on %s: %s", key_id, record.get("node"), errors)
        return _LATER
    await asyncio.to_thread(vault.vault_kv_put, path, {**record, "revoked_at": now.isoformat()})
    return _REVOKED


def _audit(key_id: str, record: dict, errors: List[str]) -> None:
    activity_logger.log_activity(
        action="user.ssh_key.expire",
        target_type="ssh_key",
        target_id=key_id,
        target_host=record.get("node"),
        target_project=record.get("project"),
        result="failure" if errors else "success",
        error_message="; ".join(errors)[:500] if errors else None,
        details={"engine": record.get("engine") or client_identities.LXD, "owner_email": record.get("owner_email")},
    )


def _shared_redis() -> Any:
    from app.core.cache import get_cache_manager

    return getattr(get_cache_manager(), "redis_client", None)


async def _loop(session_maker: Callable[[], Any], service_factory: Callable[[], Any], vault_factory: Callable[[], Any]) -> None:
    lease = CollectorLease(LEASE_KEY, int(INTERVAL_SECONDS * 2 + 15), _shared_redis)
    await asyncio.sleep(random.uniform(20.0, 60.0))
    try:
        while True:
            try:
                if await asyncio.to_thread(lease.claim):
                    done = await sweep(session_maker, service=service_factory(), vault=vault_factory())
                    if done:
                        logger.info("Expired keys revoked on their clusters: %s", done)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a blip must not end the loop
                logger.warning("Key expiry sweep failed", exc_info=True)
            await asyncio.sleep(INTERVAL_SECONDS + random.uniform(0.0, 30.0))
    finally:
        try:
            lease.release()
        except Exception:  # noqa: BLE001
            pass


def start_my_keys_expiry(
    session_maker: Callable[[], Any], service_factory: Callable[[], Any], vault_factory: Callable[[], Any]
) -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.get_running_loop().create_task(_loop(session_maker, service_factory, vault_factory))


async def stop_my_keys_expiry() -> None:
    global _task
    task, _task = _task, None
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
