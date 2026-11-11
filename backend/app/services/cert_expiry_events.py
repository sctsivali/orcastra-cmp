"""Cluster certificate expiry as operational events, so a partner hears of it before the cluster
refuses the dashboard.

The Warnings page computes expiry when someone opens it, so nothing warns ahead of time, and a
lapsed certificate makes the cluster refuse the dashboard until someone repairs trust on a host.
Every six hours one worker (Redis lease) reads each registered cluster's client
certificate and raises `cluster.certificate_expiring` (warning within 14 days, critical within
3) or `cluster.certificate_expired`, resolved once the certificate is renewed. Cluster events
reach the partners on that cluster (core/task_scope.py).

important: a pass is skipped while Vault is sealed, unreachable or refusing the dashboard's
token, and stops at the first store failure, so an outage never reads as expired certificates.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Optional

from app.core import vault_probe, vault_token
from app.core.config import get_settings
from app.services import ops_events
from app.services.collector_leadership import CollectorLease
from app.services.lxd_certs.inspect import WARNING_THRESHOLDS, parse_certificate_pem

logger = logging.getLogger(__name__)

LEASE_KEY = "orcastra:cluster_cert_expiry:lease"
INTERVAL_SECONDS = 6 * 3600
WARN_DAYS = WARNING_THRESHOLDS["warning"]
CRITICAL_DAYS = WARNING_THRESHOLDS["critical"]

_task: Optional[asyncio.Task] = None


def _vault_serves() -> bool:
    if vault_probe.seal_probe().state != "ok":
        return False
    return vault_token.status().state not in ("refused", "unknown")


def check_cluster(cluster_id: str, display_name: str, cert_pem: str, *, now: Optional[datetime] = None) -> None:
    """Raise or resolve one cluster's expiry events from its certificate."""
    not_after = (parse_certificate_pem(cert_pem) or {}).get("not_after") if cert_pem else None
    if not not_after:
        return
    now = now or datetime.now(timezone.utc)
    left = (not_after - now).total_seconds()
    days = int(left // 86400)
    facts = {"cluster": display_name, "not_after": not_after.strftime("%Y-%m-%d %H:%M UTC"), "days": max(days, 0)}
    if left <= 0:
        ops_events.clear("cluster.certificate_expiring", cluster_id=cluster_id)
        ops_events.fire("cluster.certificate_expired", cluster_id=cluster_id, facts=facts)
    elif days <= CRITICAL_DAYS:
        ops_events.fire("cluster.certificate_expiring", cluster_id=cluster_id, severity="critical", facts=facts)
    elif days <= WARN_DAYS:
        ops_events.fire("cluster.certificate_expiring", cluster_id=cluster_id, severity="warning", facts=facts)
    else:
        ops_events.resolve("cluster.certificate_expiring", cluster_id=cluster_id, facts=facts)
        ops_events.resolve("cluster.certificate_expired", cluster_id=cluster_id, facts=facts)


def run_once(cert_manager=None, *, now: Optional[datetime] = None) -> int:
    """One pass over the registry. Returns how many clusters were checked. Blocking."""
    settings = get_settings()
    if not settings.vault_enabled or not settings.vault_addr:
        return 0
    if cert_manager is None:
        from app.services.lxd.lxd_service import get_lxd_service

        cert_manager = get_lxd_service().cert_manager
    if not _vault_serves():
        logger.info("Certificate expiry pass skipped: Vault is not serving the dashboard")
        return 0
    registry = cert_manager.read_registry(use_cache=False)
    if not registry.readable:
        return 0
    checked = 0
    base = settings.vault_clusters_path
    for cluster_id in registry.cluster_ids:
        try:
            metadata = cert_manager.vault.vault_read(f"{base}/{cluster_id}/metadata") or {}
            certs = cert_manager.vault.vault_read(f"{base}/{cluster_id}/certs") or {}
        except Exception as exc:  # noqa: BLE001 - one cluster's record must not end the pass
            if vault_token.store_failed(exc):
                logger.warning("Certificate expiry pass stopped: Vault failed part way (%s)", type(exc).__name__)
                return checked
            continue
        check_cluster(cluster_id, metadata.get("display_name") or cluster_id, certs.get("client.crt") or "", now=now)
        checked += 1
    return checked


def _shared_redis():
    from app.core.cache import get_cache_manager

    return getattr(get_cache_manager(), "redis_client", None)


async def _loop() -> None:
    lease = CollectorLease(LEASE_KEY, INTERVAL_SECONDS * 2 + 60, _shared_redis)
    await asyncio.sleep(random.uniform(30.0, 120.0))
    try:
        while True:
            try:
                if await asyncio.to_thread(lease.claim):
                    checked = await asyncio.to_thread(run_once)
                    logger.debug("Certificate expiry pass checked %s clusters", checked)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a blip must not end the loop
                logger.warning("Certificate expiry pass failed", exc_info=True)
            await asyncio.sleep(INTERVAL_SECONDS)
    finally:
        try:
            lease.release()
        except Exception:  # noqa: BLE001
            pass


def start() -> None:
    global _task
    if not get_settings().vault_enabled:
        return
    if _task is None or _task.done():
        _task = asyncio.get_running_loop().create_task(_loop())


async def stop() -> None:
    global _task
    task, _task = _task, None
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
