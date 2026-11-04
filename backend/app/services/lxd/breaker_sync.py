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

Failures are shared the same way (2026-11-04). Every worker's healer redialed each dead cluster
once its own 60 s trip lapsed, and a fleet read that got there first paid the dial on a request
(about 8 s against a silent host on dev). Now only the collector's leader probes clusters it
holds down, every DEAD_PROBE_INTERVAL_S, past its own trip, and publishes what it observed as a
"down at" marker carrying only the time and the kind. A follower's healer adopts a fresh marker
instead of dialing (health.adopt), and learns of a recovery from the proof as before. A proof as
new as or newer than a down marker always wins, a malformed marker is ignored, and a follower
falls back to its own redials as soon as no fresh marker is left: a leader that stopped probing,
or no Redis, leaves every worker exactly as it was before.
"""

import asyncio
import logging
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, List, Optional, Tuple

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

DOWN_NAMESPACE = "lxd_cluster_down"
# Two probe intervals and then some: a marker outlives one missed probe, not a leader that stopped.
DOWN_TTL_S = 120
_DOWN_KINDS = (health.KIND_OFFLINE, health.KIND_UNAUTHORIZED)
PROBE_WORKERS = 4
# Per cluster. A silent host costs a 5 s pin handshake plus a 3 s connect.
PROBE_WALL_S = 20.0


def dead_probe_interval() -> float:
    """How often the leader redials the clusters it holds down. Owner decision 2026-11-04: 30 s."""
    try:
        return max(5.0, float(os.environ.get("ORCASTRA_DEAD_PROBE_SECONDS", "30")))
    except ValueError:
        return 30.0


def shared_mode() -> bool:
    """Whether failures are shared: a reachable Redis, and not switched off."""
    if os.environ.get("ORCASTRA_FLEET_VERDICT_SHARED", "true").strip().lower() in ("0", "false", "no"):
        return False
    from app.core.cache import get_cache_manager

    return getattr(get_cache_manager(), "redis_client", None) is not None

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lxd-breaker-sync")
# important: its own thread. The announce runs inside connect_with_vault_cert, which
# _try_rotated_certificate calls under a per-cluster lock; a hung Redis must not hold that lock.
_announcer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lxd-breaker-announce")
_prober = ThreadPoolExecutor(max_workers=PROBE_WORKERS, thread_name_prefix="lxd-dead-probe")
_task: Optional["asyncio.Task[None]"] = None
_last_probe_at = 0.0


def _write_marker(cluster_id: str, proven_at: float) -> None:
    from app.core.cache import get_cache_manager

    cache = get_cache_manager()
    try:
        cache.set(PROVEN_NAMESPACE, cluster_id, proven_at, ttl=PROVEN_TTL_S)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not announce %s as reachable: %s", cluster_id, exc)
    # A proof lifts the fleet's down verdict at once. Its own try: the proof above already landed,
    # and a cache double without delete must not undo it.
    try:
        delete = getattr(cache, "delete", None)
        if delete is not None:
            delete(DOWN_NAMESPACE, cluster_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not lift the down marker for %s: %s", cluster_id, exc)


def _write_down(cluster_id: str, observed_at: float, kind: str) -> None:
    from app.core.cache import get_cache_manager

    try:
        get_cache_manager().set(
            DOWN_NAMESPACE, cluster_id, {"at": observed_at, "kind": kind}, ttl=DOWN_TTL_S
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not publish %s as down: %s", cluster_id, exc)


def publish_down(cluster_id: str, kind: str, observed_at: float) -> None:
    """The leader's probe saw `cluster_id` fail. Never blocks or raises.

    important: only probe_dead_once calls this (test_only_the_leader_publishes_a_down_verdict).
    A request's own failure is never shared: a fleet verdict is a background observation, so no
    caller can mark a cluster down for every worker by sending reads.
    `observed_at` is when the probe started, so a proof made while it ran outranks it.
    """
    if kind not in _DOWN_KINDS:
        return
    try:
        _announcer.submit(_write_down, cluster_id, observed_at, kind)
    except RuntimeError:  # the executor is gone at interpreter shutdown
        pass


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


def shared_verdict(cache: Any, cluster_id: str) -> Optional[Tuple[float, str]]:
    """(observed_at, kind) of the fleet's down verdict on a cluster, or None.

    None for anything that is not a well-formed recent observation, and whenever a proof is as new
    or newer: None means "no verdict", which only ever leads to a dial.
    """
    try:
        value = cache.get(DOWN_NAMESPACE, cluster_id)
    except Exception:  # noqa: BLE001 - unreadable means no verdict
        return None
    if not isinstance(value, dict):
        return None
    kind = value.get("kind")
    try:
        observed_at = float(value.get("at"))
    except (TypeError, ValueError):
        return None
    if kind not in _DOWN_KINDS or not math.isfinite(observed_at):
        return None
    if observed_at > time.time() + MAX_CLOCK_SKEW_S:
        return None
    proven_at = _proven_at(cache, cluster_id)
    if proven_at is not None and proven_at >= observed_at:
        return None
    return observed_at, kind


def adopt_shared(cluster_id: str) -> bool:
    """Blocking. True when this worker now holds the fleet's down verdict on the cluster."""
    if not shared_mode():
        return False
    from app.core.cache import get_cache_manager

    verdict = shared_verdict(get_cache_manager(), cluster_id)
    if verdict is None:
        return False
    return health.adopt(cluster_id, verdict[0], verdict[1])


def _probe_one(lxd_service: Any, cluster_id: str) -> bool:
    started = time.time()
    try:
        if lxd_service.ensure_connection(cluster_id, force=True):
            return True
    except (LXDTransportPinError, LXDLocalStorageError) as exc:
        # Not reachability (see sync_once): never published either.
        logger.warning("Dead-cluster probe could not reach %s: %s", cluster_id, exc)
        return False
    except Exception as exc:  # noqa: BLE001 - ensure_connection catches the rest itself
        logger.warning("Dead-cluster probe of %s failed: %s", cluster_id, exc)
    verdict = health.last_failure(cluster_id)
    if verdict is not None:
        publish_down(cluster_id, verdict[1], started)
    return False


def probe_dead_once(lxd_service: Any) -> List[str]:
    """Blocking. Redial, side by side, every cluster this worker holds down; returns the ones back."""
    clusters = sorted(health.failure_times())
    if not clusters:
        return []
    futures = {cluster_id: _prober.submit(_probe_one, lxd_service, cluster_id) for cluster_id in clusters}
    recovered = []
    for cluster_id, future in futures.items():
        try:
            if future.result(timeout=PROBE_WALL_S):
                recovered.append(cluster_id)
        except Exception as exc:  # noqa: BLE001 - one cluster's probe never stops the others
            logger.debug("Dead-cluster probe of %s did not finish: %s", cluster_id, exc)
    if recovered:
        logger.info("Dead-cluster probe reconnected %s", recovered)
        try:
            from app.core.cache import get_cache_manager

            get_cache_manager().invalidate_cluster_reachability(*recovered)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Reachability cache invalidation after the dead-cluster probe failed: %s", exc)
    return recovered


def _leading() -> bool:
    try:
        from app.services.metrics_collector import get_metrics_collector

        return bool(getattr(get_metrics_collector(), "_leading", False))
    except Exception:  # noqa: BLE001
        return False


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


async def _probe_if_leading() -> None:
    global _last_probe_at
    from app.services.lxd.lxd_service import get_lxd_service

    if time.monotonic() - _last_probe_at < dead_probe_interval():
        return
    if not _leading() or not shared_mode():
        return
    _last_probe_at = time.monotonic()
    try:
        await run_blocking(
            probe_dead_once, get_lxd_service(),
            timeout=SYNC_TIMEOUT_S, label="lxd.dead_probe", executor=_executor, max_in_flight=1,
        )
    except asyncio.CancelledError:
        raise
    except BlockingPoolFull:
        logger.debug("Dead-cluster probe skipped: the sync thread is still busy")
    except Exception:  # noqa: BLE001 - the loop outlives a bad pass
        logger.warning("Dead-cluster probe pass failed", exc_info=True)


async def _loop(interval: float) -> None:
    from app.services.lxd.lxd_service import get_lxd_service

    while True:
        await asyncio.sleep(interval)
        # important: a local dict read first. A worker with nothing failed does no I/O at all.
        if not health.failure_times():
            continue
        await _probe_if_leading()
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
