"""
Release the other workers' breakers once one worker proves a cluster reachable again.

The breaker (services/lxd/health.py) is per process, and prod runs four uvicorn workers. When a
cluster that refused every certificate is repaired, only the worker that served Check
Authentication or Verify reconnects. The others kept answering 503 until their own trip expired
and the 30s healer reconnected them: about 37s measured in the lab, up to 90s in theory.

Every trusted connect now leaves a shared "proven at" marker. Each worker runs a short loop that
retries, through the healer's own door (`ensure_connection`), any cluster it last saw fail
before that marker. `health.clear` drops the failure time before the retry, so a cluster is
retried once per marker and a cluster that stays broken cannot cause a storm.

note: without Redis the marker lands in the announcing worker's memory only, which leaves every
other worker exactly where it was before this module existed. That is the fallback, not a new
failure mode. A worker with nothing failed does no cache I/O; one with a cluster that stays dead
reads one marker per such cluster per pass.
"""

import asyncio
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, List, Optional

from app.core.blocking import BlockingPoolFull, run_blocking
from app.services.lxd import health
from app.services.lxd.exceptions import LXDLocalStorageError, LXDTransportPinError

logger = logging.getLogger(__name__)

PROVEN_NAMESPACE = "lxd_cluster_proven"
# Outlives a full trip plus a healer pass, so a worker that was mid-retry still sees it.
PROVEN_TTL_S = 300
SYNC_INTERVAL_S = 5.0
SYNC_TIMEOUT_S = 60.0
# A marker further ahead than this is not a proof from this host's clock. Honouring it would
# retry every failed cluster on every pass until it expired.
MAX_CLOCK_SKEW_S = 5.0

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lxd-breaker-sync")
# important: its own thread. The announce runs inside connect_with_vault_cert, which
# _try_rotated_certificate calls under a per-cluster lock; a hung Redis must not hold that lock.
_announcer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lxd-breaker-announce")
_task: Optional["asyncio.Task[None]"] = None


def _write_marker(cluster_id: str, proven_at: float) -> None:
    from app.core.cache import get_cache_manager

    try:
        get_cache_manager().set(PROVEN_NAMESPACE, cluster_id, proven_at, ttl=PROVEN_TTL_S)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not announce %s as reachable: %s", cluster_id, exc)


def announce_proven(cluster_id: str) -> None:
    """Never blocks or raises. The time is taken now, at the proof, not when the write lands."""
    try:
        _announcer.submit(_write_marker, cluster_id, time.time())
    except RuntimeError:  # the executor is gone at interpreter shutdown
        pass


def _proven_at(cache: Any, cluster_id: str) -> Optional[float]:
    try:
        value = cache.get(PROVEN_NAMESPACE, cluster_id)
        proven_at = float(value) if value is not None else None
    except Exception:  # noqa: BLE001 - unreadable means "not proven", the pre-sync behaviour
        return None
    if proven_at is None or not math.isfinite(proven_at) or proven_at > time.time() + MAX_CLOCK_SKEW_S:
        return None
    return proven_at


def due_for_retry() -> List[str]:
    """Clusters this worker last saw fail before another worker proved them reachable."""
    failures = health.failure_times()
    if not failures:
        return []
    from app.core.cache import get_cache_manager

    cache = get_cache_manager()
    due = []
    for cluster_id, failed_at in failures.items():
        proven_at = _proven_at(cache, cluster_id)
        if proven_at is not None and proven_at > failed_at:
            due.append(cluster_id)
    return due


def sync_once(lxd_service: Any) -> List[str]:
    """Blocking. Retry every due cluster once; returns the ones that came back."""
    recovered = []
    for cluster_id in due_for_retry():
        kind = health.get_kind(cluster_id)
        reason = health.get_reason(cluster_id)
        health.clear(cluster_id)
        try:
            if lxd_service.ensure_connection(cluster_id):
                recovered.append(cluster_id)
                continue
        except (LXDTransportPinError, LXDLocalStorageError) as exc:
            # Never recorded anywhere in this codebase: a pin violation is not reachability, and a
            # local storage fault never contacted the peer. See test_storage_faults_never_trip_the_breaker.
            logger.warning("Breaker sync could not reconnect %s: %s", cluster_id, exc)
            continue
        except Exception as exc:  # noqa: BLE001 - ensure_connection catches the rest itself
            logger.warning("Breaker sync could not reconnect %s: %s", cluster_id, exc)
            continue
        # A False that recorded nothing would otherwise lift a live trip and let every request pay
        # the connect timeout again. Restore exactly what was there, kind included, so a refused
        # cluster never turns "offline".
        if kind is not None and cluster_id not in health.failure_times():
            health.record_failure(cluster_id, reason or "", kind=kind)
    if recovered:
        logger.info("Breaker sync reconnected %s after another worker proved them", recovered)
        try:
            from app.core.cache import get_cache_manager

            # One pass, one invalidation: it clears fleet-wide lists, not one cluster's keys.
            get_cache_manager().invalidate_cluster_reachability(*recovered)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Reachability cache invalidation after breaker sync failed: %s", exc)
    return recovered


async def _loop(interval: float) -> None:
    from app.services.lxd.lxd_service import get_lxd_service

    while True:
        await asyncio.sleep(interval)
        # important: a local dict read first. A worker with nothing failed does no I/O at all.
        if not health.failure_times():
            continue
        try:
            await run_blocking(
                sync_once, get_lxd_service(),
                timeout=SYNC_TIMEOUT_S, label="lxd.breaker_sync", executor=_executor,
                max_in_flight=1,
            )
        except asyncio.CancelledError:
            raise
        except BlockingPoolFull:
            # A pass outlived its timeout (a hung Redis) and still holds the thread. Skip, do not queue.
            logger.debug("Breaker sync pass skipped: the previous one is still running")
        except Exception:  # noqa: BLE001 - the loop outlives a bad pass
            logger.warning("Breaker sync pass failed", exc_info=True)


def start_breaker_sync_loop(*, interval: float = SYNC_INTERVAL_S) -> None:
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_loop(interval), name="lxd-breaker-sync")


def stop_breaker_sync_loop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
