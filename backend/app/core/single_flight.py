"""
Per-key single-flight for expensive cache rebuilds.

The cached endpoints fan out across every cluster, so N requests that miss
together would launch N fan-outs against the same hardware. This serialises
rebuilds per cache key: later arrivals wait for the first rebuild and read its
result, on the ordinary miss path and on forced bypasses alike.

The decorator holds the lock across a cache hit too. Handlers served through
core/swr.serve() read the cache first and call run_collapsed() only for a real
miss or a forced refresh, so a warm reader never queues behind a rebuild.
Scoped to one worker process: the worst case is one rebuild per worker.
"""

import asyncio
import functools
import logging
import time
from typing import Awaitable, Callable, Dict, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


_locks: Dict[str, asyncio.Lock] = {}
_waiters: Dict[str, int] = {}
_last_rebuild: Dict[str, float] = {}


def rebuild_lock(key: str) -> asyncio.Lock:
    """
    Lock guarding one cache key's rebuild. Pair every call with `release_lock`.

    Created lazily and dropped once nobody holds or awaits it, so a long-lived
    process does not accumulate one lock per scope key seen. The bookkeeping
    needs no mutex because asyncio does not preempt it.
    """
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    _waiters[key] = _waiters.get(key, 0) + 1
    return lock


def release_lock(key: str) -> None:
    """Drop the bookkeeping entry once the last waiter for `key` is done."""
    remaining = _waiters.get(key, 1) - 1
    if remaining <= 0:
        _waiters.pop(key, None)
        _locks.pop(key, None)
    else:
        _waiters[key] = remaining


def mark_rebuilt(key: str) -> None:
    """Record that a rebuild for `key` finished just now."""
    _last_rebuild[key] = time.monotonic()


def rebuilt_since(key: str, arrived_at: float) -> bool:
    """
    True when someone finished rebuilding `key` after `arrived_at`.

    This lets a forced refresh tell "another request just did this work" apart
    from "a warm entry predates this request". Without it a bypass returns the
    cache it was asked to skip, and on a prewarmed key never rebuilds at all.
    """
    return _last_rebuild.get(key, 0.0) > arrived_at


def is_rebuilding(key: str) -> bool:
    """True while a request in this worker holds or awaits the rebuild lock for `key`."""
    return _waiters.get(key, 0) > 0


async def run_collapsed(
    key: str,
    *,
    forced: bool,
    arrived_at: float,
    rebuild: Callable[[], Awaitable[T]],
    reread: Callable[[], Optional[T]],
) -> T:
    """The decorator's collapse, for a caller that has already read its cache and missed.

    Same contract as single_flight below: an unforced caller re-reads inside the lock and
    takes the body the leader just wrote; a forced caller whose leader already rebuilt after
    it arrived takes that rebuild; otherwise it rebuilds, and only a forced rebuild that
    finished marks the key. `reread` returns None when nothing usable is cached.
    """
    lock = rebuild_lock(key)
    try:
        async with lock:
            if not forced or rebuilt_since(key, arrived_at):
                if forced:
                    logger.info("Serving %s from a concurrent rebuild", key)
                cached = reread()
                if cached is not None:
                    return cached
                # The leader wrote nothing (its answer was not cacheable): rebuild, unmarked.
                return await rebuild()
            result = await rebuild()
            mark_rebuilt(key)
            return result
    finally:
        release_lock(key)


def single_flight(key_prefix: str):
    """
    Serialise rebuilds of one endpoint, per scope, so N simultaneous callers cost one
    fan-out. Applies to forced bypasses and to ordinary TTL expiry alike.

    A waiter re-runs the handler once it holds the lock; by then the leader has filled
    the cache, and if the leader failed the waiter takes the ordinary miss path. A forced
    waiter whose leader rebuilt after it arrived runs with fresh=False.

    functools.wraps keeps the signature, and FastAPI's inspect.signature follows
    __wrapped__, so the handler keeps its query parameters, dependencies and response model.
    """

    def decorator(handler):
        @functools.wraps(handler)
        async def wrapper(*args, **kwargs):
            from app.core.org_scope import get_scope_cache_key

            # Only the scalar arguments distinguish one caller's rebuild from another's;
            # dependencies like the LXD service are shared and must not enter the key.
            parts = [
                f"{name}={value}"
                for name, value in sorted(kwargs.items())
                if name != "fresh" and isinstance(value, (str, int, bool, type(None)))
            ]
            key = get_scope_cache_key(f"{key_prefix}:" + ":".join(parts))
            forced = bool(kwargs.get("fresh"))

            arrived_at = time.monotonic()
            lock = rebuild_lock(key)
            try:
                async with lock:
                    if forced and rebuilt_since(key, arrived_at):
                        # INFO level: it fires only when forced refreshes collide, and
                        # it is the only evidence that the collapse works.
                        logger.info("Serving %s from a concurrent rebuild", key)
                        return await handler(*args, **{**kwargs, "fresh": False})
                    result = await handler(*args, **kwargs)
                    # important: only a forced rebuild that finished marks the key. A leader
                    # that raised, a waiter that did no work, or an unforced call that may
                    # have been a cache hit would each let the next forced refresh serve
                    # the cache it asked to bypass.
                    if forced:
                        mark_rebuilt(key)
                    return result
            finally:
                release_lock(key)

        return wrapper

    return decorator
