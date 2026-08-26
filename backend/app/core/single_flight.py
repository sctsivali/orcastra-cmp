"""
Per-key single-flight for expensive cache rebuilds.

A manual refresh deliberately bypasses the response cache, which is correct for
one operator but dangerous in aggregate: the endpoints worth refreshing are the
ones that fan out across every cluster, and N simultaneous bypasses would launch
N fan-outs against the same hardware. This serialises them per cache key, so the
second arrival waits for the first rebuild and then reads its result.

important: this guards the ordinary cache-miss path too, not only forced
bypasses. It used to return early unless fresh was set, which left the far more
common collision unguarded: every response cache here has a TTL, so when it
expires, every request in flight for that scope misses together and each one
launches its own full fan-out. Forced refreshes are rate limited to 12 a minute
per subject; TTL expiry is not limited by anything, and it lands hardest exactly
when the most tabs are open. Polling clients make this worse, because queries
created in the same tick share a deadline for the life of the page.

The lock is held across a cache hit as well, which costs one Redis GET of
serialisation per scope and is what keeps the fast path from needing to know
whether it is about to hit or miss.

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


def single_flight(key_prefix: str):
    """
    Serialise rebuilds of one endpoint, per scope, so N simultaneous callers cost one
    fan-out instead of N. Applies to forced bypasses and to ordinary TTL expiry alike.

    The rate limit bounds a single operator's forced refreshes; it does nothing about
    ten operators clicking at the same moment, and nothing at all about a TTL lapsing
    under a dozen open tabs. Both mean parallel fan-outs across every cluster in scope.

    important: the waiter re-runs the same handler rather than duplicating its logic.
    By the time it holds the lock the leader has populated the cache, so a normal
    cached read returns the freshly rebuilt body. If the leader failed and wrote
    nothing, the waiter simply takes the ordinary cache-miss path. A forced waiter is
    downgraded to fresh=False because the leader's rebuild is the thing it wanted.

    note: the signature is preserved through functools.wraps, and FastAPI resolves
    parameters with inspect.signature, which follows __wrapped__. The decorated
    handler keeps its query parameters, dependencies and response model.
    """

    def decorator(handler):
        @functools.wraps(handler)
        async def wrapper(*args, **kwargs):
            from app.core.org_scope import get_scope_cache_key

            # only the scalar arguments distinguish one caller's rebuild from another's;
            # dependencies like the LXD service are shared and must not enter the key
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
                    #
                    # An unforced call must never mark: it cannot tell a rebuild from a
                    # cache hit, and claiming a hit as a rebuild would let it fob off the
                    # next forced refresh with the very body that refresh meant to skip.
                    if forced:
                        mark_rebuilt(key)
                    return result
            finally:
                release_lock(key)

        return wrapper

    return decorator
