"""LXD /1.0/metrics, read once per member (or per member and project) and shared by every worker.

A scrape without `project` carries every instance of every project on that member, so the
dashboard's sweep and every open instance dialog on that member share one read. A project's own
scrape is the fallback for a member whose whole read is refused or too large. Readings are cached
for 10 seconds through the shared cache manager.

important: LXD itself caches /1.0/metrics for 8 seconds. Keeping our cache longer than that
means every scrape we make lands after LXD's cache has expired, so the counters in it were
collected during our request and the scrape time can stand in for the sample time of the disk
I/O counters. With a shorter cache two of our reads could straddle one LXD refresh, and a
counter that moved over 8 seconds would be divided by a fraction of a second.

note: a cached body holds the series of every instance on that member (or in the project on
that member), across every tenant there. It never leaves the backend; callers take only the
instances they were authorized for.
"""

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, Optional, Tuple, Union
from urllib.parse import quote

from pylxd.exceptions import LXDAPIException
from requests import exceptions as requests_exc
from urllib3 import exceptions as urllib3_exc

from app.core.member_names import is_member_name, is_standalone_location

from .metrics_text import InstanceSeries, parse_instance_series, parse_member_series

logger = logging.getLogger(__name__)

CACHE_NAMESPACE = "lxd_metrics"
MEMBER_CACHE_NAMESPACE = "lxd_member_metrics"
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
_CHUNK_BYTES = 64 * 1024

_flight_locks: Dict[str, threading.Lock] = {}
_flight_users: Dict[str, int] = {}
_flight_guard = threading.Lock()


# A member-wide read refused for these reasons may still be allowed one project at a time.
PROJECT_FALLBACK = frozenset({"refused", "too_large"})


@dataclass(frozen=True)
class ProjectScrape:
    collected_at: float
    series: Dict[str, InstanceSeries]


@dataclass(frozen=True)
class MemberScrape:
    collected_at: float
    series: Dict[Tuple[str, str], InstanceSeries]

    def project(self, project: str) -> ProjectScrape:
        """This scrape as the named project's own, for a reader of one project's instances."""
        return ProjectScrape(
            self.collected_at, {name: data for (owner, name), data in self.series.items() if owner == project}
        )


Scrape = Union[ProjectScrape, MemberScrape]


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


def _key(*parts: Optional[str]) -> str:
    return ":".join(quote(part or "", safe="") for part in parts)


def _project_entry(entry: Dict[str, Any]) -> ProjectScrape:
    return ProjectScrape(
        collected_at=float(entry["t"]),
        series={str(name): InstanceSeries.from_json(data) for name, data in dict(entry["s"]).items()},
    )


def _member_entry(entry: Dict[str, Any]) -> MemberScrape:
    return MemberScrape(
        collected_at=float(entry["t"]),
        series={(str(owner), str(name)): InstanceSeries.from_json(data) for owner, name, data in entry["s"]},
    )


def _cached(cache: Any, namespace: str, key: str, decode: Callable[[Dict[str, Any]], Scrape]) -> Optional[object]:
    """A scrape, the failure kind (a string) for a cached failure, or None for a miss."""
    try:
        entry = cache.get(namespace, key)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Metrics cache read failed for %s: %s", key, exc)
        return None
    if not isinstance(entry, dict):
        return None
    if entry.get("failed"):
        return str(entry["failed"])
    try:
        return decode(entry)
    except (KeyError, TypeError, ValueError):
        return None


def _store(cache: Any, namespace: str, key: str, value: Dict[str, Any], ttl: int) -> None:
    try:
        cache.set(namespace, key, value, ttl)
    except Exception as exc:  # noqa: BLE001 - a failed cache write costs one more scrape
        logger.debug("Metrics cache write failed for %s: %s", key, exc)


def _fail(cache: Any, namespace: str, key: str, kind: str, detail: object) -> str:
    logger.info(
        "Skipping LXD metrics for %s for %ss: %s (%s)",
        key[:160],
        NEGATIVE_TTL_SECONDS[kind],
        kind,
        str(detail)[:200],
    )
    _store(cache, namespace, key, {"failed": kind}, NEGATIVE_TTL_SECONDS[kind])
    return kind


def _supports_metrics(client: Any) -> bool:
    try:
        return "metrics" in (client.host_info or {}).get("api_extensions", [])
    except Exception:  # noqa: BLE001
        return False


def _fetch_text(
    client: Any,
    cache: Any,
    namespace: str,
    key: str,
    member: Optional[str],
    params: Dict[str, str],
    deadline: float,
) -> Union[Tuple[str, float], str]:
    """(text, collected_at), or the failure kind after caching it."""
    if not _supports_metrics(client):
        return _fail(cache, namespace, key, "unsupported", "no metrics API extension")
    params = dict(params)
    if not is_standalone_location(member):
        # important: a clustered member reports only its own instances, so the read must be
        # forwarded to the member the instance runs on.
        params["target"] = member or ""
    read_timeout = max(1.0, min(4.0, deadline - time.monotonic() - 1.5))
    started = time.time()
    try:
        # note: identity, so the byte cap below counts what gets parsed rather than what a
        # compressed body could expand to.
        response = client.api.metrics.get(
            params=params,
            is_api=False,
            timeout=(2, read_timeout),
            stream=True,
            headers={"Accept-Encoding": "identity"},
        )
    except (requests_exc.Timeout, requests_exc.ConnectionError) as exc:
        return _fail(cache, namespace, key, "timeout", exc)
    except LXDAPIException as exc:
        # important: never str(exc). pylxd's __str__ reads the whole error body, which is
        # exactly the unbounded read the streaming below exists to prevent.
        status = getattr(exc.response, "status_code", None)
        close = getattr(exc.response, "close", None)
        if callable(close):
            close()
        return _fail(cache, namespace, key, "refused", f"HTTP {status}")
    # important: streamed against a byte cap and a wall-clock budget. Reading the whole body
    # first held any size an endpoint chose to send, and a slow drip kept the thread as long
    # as it liked, one read timeout at a time.
    try:
        declared = int(response.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        declared = 0
    if declared > MAX_BODY_BYTES:
        response.close()
        return _fail(cache, namespace, key, "too_large", f"{declared} bytes declared")
    encoding = str(response.headers.get("Content-Encoding") or "identity").strip().lower()
    if encoding not in ("identity", ""):
        response.close()
        return _fail(cache, namespace, key, "unparseable", f"content encoding {encoding[:40]}")
    chunks: list[bytes] = []
    received = 0
    stop_at = deadline - 1.0
    try:
        # important: read1, not iter_content. iter_content waits for a full chunk, so a body
        # dripping one byte inside each socket timeout held the thread for hours between
        # deadline checks; read1 returns whatever arrived and the clock is checked every time.
        while True:
            chunk = response.raw.read1(_CHUNK_BYTES)
            if not chunk:
                break
            received += len(chunk)
            if received > MAX_BODY_BYTES:
                return _fail(cache, namespace, key, "too_large", f"over {MAX_BODY_BYTES} bytes")
            if time.monotonic() > stop_at:
                return _fail(cache, namespace, key, "timeout", "body still arriving at the deadline")
            chunks.append(chunk)
    except (requests_exc.RequestException, urllib3_exc.HTTPError, OSError) as exc:
        return _fail(cache, namespace, key, "timeout", exc)
    finally:
        response.close()
    collected_at = (started + time.time()) / 2
    if time.monotonic() > stop_at:
        return _fail(cache, namespace, key, "timeout", "no budget left to parse")
    # important: UTF-8 whatever the endpoint declares. A declared charset picks the decoder,
    # and some (punycode) are quadratic in pure Python.
    return b"".join(chunks).decode("utf-8", errors="replace"), collected_at


def _scrape_project(
    client: Any, cache: Any, key: str, member: Optional[str], project: str, deadline: float
) -> Union[ProjectScrape, str]:
    fetched = _fetch_text(client, cache, CACHE_NAMESPACE, key, member, {"project": project}, deadline)
    if isinstance(fetched, str):
        return fetched
    text, collected_at = fetched
    try:
        series = parse_instance_series(text, project=project)
    except Exception as exc:  # noqa: BLE001
        return _fail(cache, CACHE_NAMESPACE, key, "unparseable", exc)
    entry = {"t": collected_at, "s": {name: data.to_json() for name, data in series.items()}}
    _store(cache, CACHE_NAMESPACE, key, entry, CACHE_TTL_SECONDS)
    return ProjectScrape(collected_at=collected_at, series=series)


def _scrape_member(
    client: Any, cache: Any, key: str, member: Optional[str], deadline: float
) -> Union[MemberScrape, str]:
    fetched = _fetch_text(client, cache, MEMBER_CACHE_NAMESPACE, key, member, {}, deadline)
    if isinstance(fetched, str):
        return fetched
    text, collected_at = fetched
    try:
        series = parse_member_series(text)
    except Exception as exc:  # noqa: BLE001
        return _fail(cache, MEMBER_CACHE_NAMESPACE, key, "unparseable", exc)
    rows = [[owner, name, data.to_json()] for (owner, name), data in series.items()]
    _store(cache, MEMBER_CACHE_NAMESPACE, key, {"t": collected_at, "s": rows}, CACHE_TTL_SECONDS)
    return MemberScrape(collected_at=collected_at, series=series)


def _shared_read(
    cluster: str,
    member: Optional[str],
    namespace: str,
    parts: Tuple[str, ...],
    decode: Callable[[Dict[str, Any]], Scrape],
    scrape: Callable[[Any, str, Optional[str]], Union[Scrape, str]],
    deadline: float,
) -> Union[Scrape, str, None]:
    """A cached or fresh scrape, the failure kind, or None when it was not attempted."""
    if deadline - time.monotonic() < MIN_BUDGET_SECONDS:
        return None
    standalone = is_standalone_location(member)
    if not standalone and not is_member_name(member):
        # An endpoint that is not behaving like LXD; the name must not reach a key, a log line or
        # a target parameter.
        logger.debug("Not scraping metrics for an unrecognisable member on %s", cluster)
        return None
    cache = _cache()
    key = _key(cluster, "none" if standalone else member, *parts)
    hit = _cached(cache, namespace, key, decode)
    if hit is not None:
        return hit  # type: ignore[return-value]
    wait = min(2.0, deadline - time.monotonic() - MIN_BUDGET_SECONDS)
    with _single_flight(f"{namespace}:{key}", wait) as owner:
        hit = _cached(cache, namespace, key, decode)
        if hit is not None:
            return hit  # type: ignore[return-value]
        if not owner:
            return None
        return scrape(cache, key, member)


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
    read = _shared_read(
        cluster, member, CACHE_NAMESPACE, (project,), _project_entry,
        lambda cache, key, target: _scrape_project(client, cache, key, target, project, deadline),
        deadline,
    )
    return read if isinstance(read, ProjectScrape) else None


def member_read(
    client: Any, cluster: str, member: Optional[str], *, deadline: float
) -> Union[MemberScrape, str, None]:
    """Every project's metrics on `member`, the failure kind, or None when not attempted."""
    read = _shared_read(
        cluster, member, MEMBER_CACHE_NAMESPACE, (), _member_entry,
        lambda cache, key, target: _scrape_member(client, cache, key, target, deadline),
        deadline,
    )
    return read  # type: ignore[return-value]


def member_metrics(client: Any, cluster: str, member: Optional[str], *, deadline: float) -> Optional[MemberScrape]:
    """Every project's metrics on `member`, or None when they cannot be had within the budget."""
    read = member_read(client, cluster, member, deadline=deadline)
    return read if isinstance(read, MemberScrape) else None


def instance_scrape(
    client: Any, cluster: str, member: Optional[str], project: str, *, deadline: float
) -> Optional[ProjectScrape]:
    """The instance dialog's scrape: the member-wide one the dashboard shares, or the project's
    own when the member-wide read is refused or too large."""
    read = member_read(client, cluster, member, deadline=deadline)
    if isinstance(read, MemberScrape):
        return read.project(project)
    if read in PROJECT_FALLBACK:
        return project_metrics(client, cluster, member, project, deadline=deadline)
    return None
