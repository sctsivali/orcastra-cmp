"""
Per-key single-flight for expensive cache rebuilds.

A manual refresh deliberately bypasses the response cache, which is correct for
one operator but dangerous in aggregate: the endpoints worth refreshing are the
ones that fan out across every cluster, and N simultaneous bypasses would launch
N fan-outs against the same hardware. This serialises them per cache key, so the
second arrival waits for the first rebuild and then reads its result.

Scoped to one worker process. With WEB_CONCURRENCY=4 the worst case is 4
concurrent rebuilds instead of one per request, which is the bound that matters.
"""

import asyncio
import functools
import logging
import time
from typing import Dict

logger = logging.getLogger(__name__)


_locks: Dict[str, asyncio.Lock] = {}
_waiters: Dict[str, int] = {}
_last_rebuild: Dict[str, float] = {}


def rebuild_lock(key: str) -> asyncio.Lock:
    """
    Lock guarding one cache key's rebuild.

    note: created lazily and dropped once nobody holds or awaits it, so a
    long-lived process does not accumulate one lock per scope key seen. The
    bookkeeping is only safe because asyncio runs this without preemption.
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

    important: this is what lets a forced refresh tell "another request just did
    the work I was about to do" apart from "there is a warm entry from before I
    arrived". Without the distinction a bypass returns the very cache it was
    asked to skip, which on a prewarmed key means it never rebuilds at all.
    """
    return _last_rebuild.get(key, 0.0) > arrived_at


def single_flight_on_fresh(key_prefix: str):
    """
    Serialise forced rebuilds of one endpoint, per scope, so N simultaneous bypasses
    cost one fan-out instead of N.

    The rate limit bounds a single operator; it does nothing about ten operators
    clicking refresh at the same moment, which on these endpoints means ten parallel
    fan-outs across every cluster in scope.

    important: the waiter re-runs the same handler with fresh=False rather than
    duplicating its logic. By the time it holds the lock the leader has populated the
    cache, so a normal cached read returns the freshly rebuilt body. If the leader
    failed and wrote nothing, the waiter simply takes the ordinary cache-miss path.

    note: the signature is preserved through functools.wraps, and FastAPI resolves
    parameters with inspect.signature, which follows __wrapped__. The decorated
    handler keeps its query parameters, dependencies and response model.
    """

    def decorator(handler):
        @functools.wraps(handler)
        async def wrapper(*args, **kwargs):
            if not kwargs.get("fresh"):
                return await handler(*args, **kwargs)

            from app.core.org_scope import get_scope_cache_key

            # only the scalar arguments distinguish one caller's rebuild from another's;
            # dependencies like the LXD service are shared and must not enter the key
            parts = [
                f"{name}={value}"
                for name, value in sorted(kwargs.items())
                if name != "fresh" and isinstance(value, (str, int, bool, type(None)))
            ]
            key = get_scope_cache_key(f"{key_prefix}:" + ":".join(parts))

            arrived_at = time.monotonic()
            lock = rebuild_lock(key)
            try:
                async with lock:
                    if rebuilt_since(key, arrived_at):
                        # note: INFO on purpose. It only fires when forced refreshes
                        # actually collide, so it is rare, and when it does fire it is
                        # the only evidence that the collapse is working.
                        logger.info("Serving %s from a concurrent rebuild", key)
                        return await handler(*args, **{**kwargs, "fresh": False})
                    result = await handler(*args, **kwargs)
                    # important: only a rebuild that finished counts as one. Marking in
                    # the finally recorded a leader that raised as a completed rebuild,
                    # so the next waiter took the fresh=False branch and served the very
                    # cache it had asked to bypass while the leader answered 503. Two
                    # operators clicking refresh during an outage got different answers.
                    # A waiter that did no work must not mark either, or the claim chains
                    # to a third request and the bypass degrades to a cached read.
                    mark_rebuilt(key)
                    return result
            finally:
                release_lock(key)

        return wrapper

    return decorator
