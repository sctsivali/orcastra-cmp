"""LXD /1.0/metrics, read once per (cluster, member, project) and shared by every worker.

One scrape carries every instance of the project on that member, so a dialog poll must not
trigger one per viewer. Readings are cached for 10 seconds through the shared cache manager.

important: LXD itself caches /1.0/metrics for 8 seconds. Keeping our cache longer than that
means every scrape we make lands after LXD's cache has expired, so the counters in it were
collected during our request and the scrape time can stand in for the sample time of the disk
I/O counters. With a shorter cache two of our reads could straddle one LXD refresh, and a
counter that moved over 8 seconds would be divided by a fraction of a second.

note: the cached body holds the series of every instance in the project on that member, across
any tenants sharing the project. It never leaves the backend; callers take only the instance
they were authorized for.
"""

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterator, Optional
from urllib.parse import quote

from pylxd.exceptions import LXDAPIException
from requests import exceptions as requests_exc

from .cpu_allocation import STANDALONE_LOCATIONS
from .metrics_text import InstanceSeries, parse_instance_series

logger = logging.getLogger(__name__)

CACHE_NAMESPACE = "lxd_metrics"
CACHE_TTL_SECONDS = 10
NEGATIVE_TTL_SECONDS = {
    "unsupported": 600,
    "refused": 600,
    "too_large": 600,
    "unparseable": 120,
    "timeout": 30,
}
MIN_BUDGET_SECONDS = 3.0
MAX_BODY_BYTES = 8 * 1024 * 1024

_flight_locks: Dict[str, threading.Lock] = {}
_flight_users: Dict[str, int] = {}
_flight_guard = threading.Lock()


@dataclass(frozen=True)
class ProjectScrape:
    collected_at: float
    series: Dict[str, InstanceSeries]


@contextmanager
def _single_flight(key: str, wait_seconds: float) -> Iterator[bool]:
    """Per-key lock for executor threads; yields False when it could not be taken in time."""
    with _flight_guard:
        lock = _flight_locks.setdefault(key, threading.Lock())
        _flight_users[key] = _flight_users.get(key, 0) + 1
    acquired = lock.acquire(timeout=max(0.0, wait_seconds))
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()
        with _flight_guard:
            remaining = _flight_users.get(key, 1) - 1
            if remaining <= 0:
                _flight_users.pop(key, None)
                _flight_locks.pop(key, None)
            else:
                _flight_users[key] = remaining


def _cache() -> Any:
    from app.core.cache import get_cache_manager

    return get_cache_manager()


def _key(cluster: str, member: Optional[str], project: str) -> str:
    return ":".join(quote(part or "", safe="") for part in (cluster, member, project))


def _cached(cache: Any, key: str) -> Optional[object]:
    """A ProjectScrape, the string "negative" for a cached failure, or None for a miss."""
    try:
        entry = cache.get(CACHE_NAMESPACE, key)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Metrics cache read failed for %s: %s", key, exc)
        return None
    if not isinstance(entry, dict):
        return None
    if entry.get("failed"):
        return "negative"
    try:
        return ProjectScrape(
            collected_at=float(entry["t"]),
            series={
                str(name): InstanceSeries.from_json(data)
                for name, data in dict(entry["s"]).items()
            },
        )
    except (KeyError, TypeError, ValueError):
        return None


def _store(cache: Any, key: str, value: Dict[str, Any], ttl: int) -> None:
    try:
        cache.set(CACHE_NAMESPACE, key, value, ttl)
    except Exception as exc:  # noqa: BLE001 - a failed cache write costs one more scrape
        logger.debug("Metrics cache write failed for %s: %s", key, exc)


def _fail(cache: Any, key: str, kind: str, detail: object) -> None:
    logger.info("Skipping LXD metrics for %s for %ss: %s (%s)", key, NEGATIVE_TTL_SECONDS[kind], kind, detail)
    _store(cache, key, {"failed": kind}, NEGATIVE_TTL_SECONDS[kind])


def _supports_metrics(client: Any) -> bool:
    try:
        return "metrics" in (client.host_info or {}).get("api_extensions", [])
    except Exception:  # noqa: BLE001
        return False


def _scrape(
    client: Any, cache: Any, key: str, member: Optional[str], project: str, deadline: float
) -> Optional[ProjectScrape]:
    if not _supports_metrics(client):
        _fail(cache, key, "unsupported", "no metrics API extension")
        return None
    params: Dict[str, str] = {"project": project}
    if (member or "") not in STANDALONE_LOCATIONS:
        # important: a clustered member reports only its own instances, so the read must be
        # forwarded to the member the instance runs on.
        params["target"] = member or ""
    read_timeout = max(1.0, min(4.0, deadline - time.monotonic() - 1.5))
    started = time.time()
    try:
        response = client.api.metrics.get(params=params, is_api=False, timeout=(2, read_timeout))
    except (requests_exc.Timeout, requests_exc.ConnectionError) as exc:
        _fail(cache, key, "timeout", exc)
        return None
    except LXDAPIException as exc:
        _fail(cache, key, "refused", exc)
        return None
    collected_at = (started + time.time()) / 2
    body = response.content or b""
    if len(body) > MAX_BODY_BYTES:
        _fail(cache, key, "too_large", f"{len(body)} bytes")
        return None
    try:
        series = parse_instance_series(response.text, project=project)
    except Exception as exc:  # noqa: BLE001
        _fail(cache, key, "unparseable", exc)
        return None
    _store(
        cache,
        key,
        {"t": collected_at, "s": {name: data.to_json() for name, data in series.items()}},
        CACHE_TTL_SECONDS,
    )
    return ProjectScrape(collected_at=collected_at, series=series)


def project_metrics(
    client: Any,
    cluster: str,
    member: Optional[str],
    project: str,
    *,
    deadline: float,
) -> Optional[ProjectScrape]:
    """The project's metrics on `member`, or None when they cannot be had within the budget.

    `deadline` is a time.monotonic() value. A failure never fails the caller's sample: memory
    then comes from the state read and disk I/O is reported unavailable.
    """
    if deadline - time.monotonic() < MIN_BUDGET_SECONDS:
        return None
    cache = _cache()
    key = _key(cluster, member, project)
    hit = _cached(cache, key)
    if hit is not None:
        return hit if isinstance(hit, ProjectScrape) else None
    wait = min(2.0, deadline - time.monotonic() - MIN_BUDGET_SECONDS)
    with _single_flight(key, wait) as owner:
        hit = _cached(cache, key)
        if hit is not None:
            return hit if isinstance(hit, ProjectScrape) else None
        if not owner:
            return None
        return _scrape(client, cache, key, member, project, deadline)
