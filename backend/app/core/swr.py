"""Stale-while-revalidate for the fan-out response caches.

Every list endpoint that fans out across clusters caches its body per scope. Before this module a
request that found the entry expired waited for the whole fan-out, and the TTLs were matched to
the frontend's polls, so a polled list missed on almost every poll: GET /instances hit 21% of the
time and each miss cost about 4 s on dev. Here an expired entry is still served at once for up
to SWR_MAX_STALE_SECONDS past its soft TTL, the same rule as RFC 5861's stale-while-revalidate,
and one background read replaces it.

Three things keep that honest:
- a mutation still invalidates at once. The generation fences and key sweeps in core/cache.py
  delete or fence the entry, and a background read that started before an invalidation is not
  allowed to write its result back;
- the response says what it is: X-Orcastra-Cache (hit | stale | miss | bypass) and
  X-Orcastra-Cache-Age, set by RequestIdMiddleware from the request context, so the frontend can
  ask again once the background read has landed and show the collection time;
- `fresh=true` is unchanged: a forced refresh always reads the clusters before it answers.

important: the key is always built here from the request's scope (get_scope_cache_key) and the
background read runs in a copy of the request's context, so it computes with exactly the same
cluster set, tenant ACL and project bound as the request that launched it. A builder must not
capture the request's database session or Request object; everything after `await serve()`
returns may outlive the request.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from dataclasses import dataclass
from time import monotonic
from typing import Any, Awaitable, Callable, Dict, Optional

from app.core.cache_envelope import Entry, unwrap, wrap
from app.core.org_scope import get_scope_cache_key
from app.core.request_context import get_request_context
from app.core.single_flight import is_rebuilding, rebuild_lock, release_lock, run_collapsed
from app.services.collector_leadership import CollectorLease

logger = logging.getLogger(__name__)

NAMESPACE = "api_responses"
# How long past its soft TTL an entry may still be served while a background read replaces it.
SWR_MAX_STALE_SECONDS = 180
# One background read per key across every worker. Released after a write; left to lapse after a
# failed read, which spaces out retries against a fleet that is not answering.
REFRESH_LEASE_SECONDS = 60
MAX_BACKGROUND_REFRESHES = 8

HIT, STALE, MISS, BYPASS = "hit", "stale", "miss", "bypass"

_refreshing: Dict[str, asyncio.Task] = {}
_in_background: contextvars.ContextVar[bool] = contextvars.ContextVar("swr_background", default=False)
# What the build running in this context would store, so a read that finishes after the answer
# went out (fanout.gather_clusters) can ask for one background read to put it in.
_building: contextvars.ContextVar[Optional[tuple]] = contextvars.ContextVar("swr_building", default=None)


@dataclass(frozen=True)
class Built:
    """What one read of the clusters produced.

    `value` is returned to the caller that built it (usually the response model); `body` is the
    JSON-ready form that is cached. `fresh_for` is the soft TTL, or None when the handler's own
    rule says this answer must not be remembered (nobody answered, a partial read).
    """

    value: Any
    body: Any
    fresh_for: Optional[int]


@dataclass(frozen=True)
class Served:
    state: str
    value: Any = None
    body: Any = None
    age: Optional[float] = None

    @property
    def built(self) -> bool:
        return self.state in (MISS, BYPASS)

    def as_model(self, model: Callable[..., Any]) -> Any:
        """The response model: the one built in this request, or one parsed from the cache."""
        if self.built:
            return self.value
        return model(**self.body)


async def serve(
    *,
    cache: Optional[Any],
    base_key: str,
    fresh: bool,
    build: Callable[[], Awaitable[Built]],
    generation: Optional[Callable[[], Any]] = None,
    label: str,
) -> Served:
    """Answer from the cache when it can, read the clusters when it must.

    `generation` returns the token(s) a mutation advances; a read during which it changed is not
    written back. Call this after the handler's own access checks: it is not an authorization
    boundary, only a cache.
    """
    if cache is None:
        # No cache configured (some handlers have always tolerated it): read, never remember.
        built = await build()
        return _record(Served(BYPASS if fresh else MISS, value=built.value, body=built.body, age=0.0))

    key = get_scope_cache_key(base_key)
    if ":scope=" not in key:
        raise RuntimeError(f"refusing to cache {label} under an unscoped key")

    if not fresh:
        entry, authoritative = _read(cache, key)
        if entry is not None:
            now = time.time()
            if entry.is_fresh(now):
                return _record(Served(HIT, body=entry.value, age=entry.age(now)))
            if authoritative and _within_stale_window(entry, now):
                _schedule_refresh(cache, key, base_key, build, generation, label)
                return _record(Served(STALE, body=entry.value, age=entry.age(now)))

    # note: the same clock single_flight marks rebuilds with; ages use wall time (time.time).
    arrived_at = monotonic()

    async def rebuild() -> Served:
        built = await _build_and_store(cache, key, base_key, build, generation, label)
        return Served(BYPASS if fresh else MISS, value=built.value, body=built.body, age=0.0)

    def reread() -> Optional[Served]:
        entry, _ = _read(cache, key)
        now = time.time()
        if entry is None or not entry.is_fresh(now):
            return None
        return Served(HIT, body=entry.value, age=entry.age(now))

    served = await run_collapsed(
        key, forced=fresh, arrived_at=arrived_at, rebuild=rebuild, reread=reread
    )
    return _record(served)


def _read(cache: Any, key: str) -> tuple[Optional[Entry], bool]:
    getter = getattr(cache, "get_entry", None)
    if getter is not None:
        result = getter(NAMESPACE, key)
        # note: a catch-all cache double answers None here rather than a pair.
        raw, authoritative = result if isinstance(result, tuple) else (result, True)
    else:
        # A cache double without get_entry: its get() hands back whatever was set.
        raw, authoritative = cache.get(NAMESPACE, key), True
    if raw is None:
        return None, authoritative
    return unwrap(raw), authoritative


def _within_stale_window(entry: Entry, now: float) -> bool:
    if entry.stored_at is None or entry.fresh_for is None:
        return False
    return now - entry.stored_at <= entry.fresh_for + SWR_MAX_STALE_SECONDS


async def _build_and_store(
    cache: Any,
    key: str,
    base_key: str,
    build: Callable[[], Awaitable[Built]],
    generation: Optional[Callable[[], Any]],
    label: str,
) -> Built:
    started = time.time()
    before = generation() if generation is not None else None
    token = _building.set((cache, key, base_key, build, generation, label))
    try:
        built = await build()
    finally:
        _building.reset(token)
    if built.fresh_for is None:
        return built
    if get_scope_cache_key(base_key) != key:
        # The scope this read computed with is not the one its key names. Never write it.
        logger.error("Not caching %s: the scope changed during the read", label)
        return built
    if generation is not None and generation() != before:
        logger.info("Not caching %s: a mutation completed during the read", label)
        return built
    current, _ = _read(cache, key)
    if current is not None and (current.build_started_at or 0.0) > started:
        # A read that started later (an operator's forced refresh) has already written.
        return built
    cache.set(
        NAMESPACE,
        key,
        wrap(built.body, stored_at=time.time(), build_started_at=started, fresh_for=built.fresh_for),
        ttl=built.fresh_for + SWR_MAX_STALE_SECONDS,
    )
    # important: the check above and this write are two Redis calls, not one. An invalidation that
    # advanced the generation and swept the key between them would otherwise leave this
    # pre-mutation body in place for the whole stale window. Checking again after the write and
    # dropping the key closes that gap; the worst case is one extra miss.
    if generation is not None and generation() != before:
        logger.info("Dropping %s: a mutation completed while it was being written", label)
        delete = getattr(cache, "delete", None)
        if delete is not None:
            delete(NAMESPACE, key)
    return built


def _schedule_refresh(
    cache: Any,
    key: str,
    base_key: str,
    build: Callable[[], Awaitable[Built]],
    generation: Optional[Callable[[], Any]],
    label: str,
) -> None:
    if key in _refreshing or is_rebuilding(key):
        return
    if len(_refreshing) >= MAX_BACKGROUND_REFRESHES:
        logger.debug("Not refreshing %s in the background: %d already running", label, len(_refreshing))
        return
    lease = CollectorLease(
        f"orcastra:swr_lease:{NAMESPACE}:{key}",
        REFRESH_LEASE_SECONDS,
        lambda: getattr(cache, "redis_client", None),
    )
    if not lease.claim():
        return
    context = contextvars.copy_context()
    context.run(_in_background.set, True)
    task = asyncio.get_running_loop().create_task(
        _refresh(cache, key, base_key, build, generation, label, lease),
        name=f"swr:{label}",
        context=context,
    )
    _refreshing[key] = task
    task.add_done_callback(lambda done: _forget(key, done))


async def _refresh(
    cache: Any,
    key: str,
    base_key: str,
    build: Callable[[], Awaitable[Built]],
    generation: Optional[Callable[[], Any]],
    label: str,
    lease: CollectorLease,
) -> None:
    started = monotonic()
    wrote = False
    # important: under the same per-key rebuild lock a miss or a forced refresh takes. Two reads of
    # one list running side by side in this worker meet in the bounded per-cluster pools below
    # them, and the second is refused: live QA saw a Refresh on /projects answer 503 ("Project list
    # for central-1 is temporarily unavailable") because it landed while the background read held
    # that cluster's slot. Holding the lock queues the refresh behind it instead; it then reads
    # the clusters itself, because a background read never marks a rebuild.
    lock = rebuild_lock(key)
    try:
        async with lock:
            built = await _build_and_store(cache, key, base_key, build, generation, label)
        wrote = built.fresh_for is not None
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - a failed background read keeps the stale entry
        logger.warning("Background refresh of %s failed: %s: %s", label, type(exc).__name__, exc)
    finally:
        release_lock(key)
        logger.info(
            "swr refresh %s %d ms wrote=%s", label, round((monotonic() - started) * 1000), wrote
        )
    if wrote:
        lease.release()


def capture_refresh() -> Optional[Callable[[], None]]:
    """A call that schedules one background read of the entry the current build would store.

    None outside a build. For a cluster read that outlived its wall: when it finishes, its result
    stands in for that cluster (fanout.gather_clusters), and this rebuild puts it into the body
    without waiting for the next poll to miss. The usual single-refresh lease applies.
    """
    building = _building.get()
    if building is None:
        return None
    cache, key, base_key, build, generation, label = building

    def schedule() -> None:
        try:
            _schedule_refresh(cache, key, base_key, build, generation, label)
        except RuntimeError:  # no running loop (shutdown)
            pass

    return schedule


def _forget(key: str, task: asyncio.Task) -> None:
    if _refreshing.get(key) is task:
        _refreshing.pop(key, None)
    if not task.cancelled():
        task.exception()


def _record(served: Served) -> Served:
    """Note what was served on the request context, for the response headers and access log."""
    if _in_background.get():
        return served
    context = get_request_context()
    if context is not None and "cache" not in context.extra:
        context.extra["cache"] = served.state
        if served.age is not None:
            context.extra["cache_age_s"] = round(served.age, 1)
    return served


async def shutdown() -> None:
    """Cancel background reads still running when the worker stops."""
    tasks = list(_refreshing.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _refreshing.clear()

