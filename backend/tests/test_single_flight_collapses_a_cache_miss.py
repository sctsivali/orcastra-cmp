"""Concurrent callers that all miss the cache must produce one fan-out between them.

When a response cache's TTL lapses, every request in flight for that scope misses
together, and each would launch its own fan-out across every cluster. Forced refreshes
are rate limited per subject; TTL expiry is limited by nothing, and polling queries
created in the same tick share a deadline for the life of the page. Both the miss path
and the forced-bypass contract are pinned here.
"""

import asyncio

from app.core import single_flight as sf


class _Cache:
    """Just enough of the cache shape the guarded handlers use."""

    def __init__(self):
        self.value = None

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def _make_handler(cache, rebuilds, rebuild_delay=0.05):
    """A handler shaped like the real ones: check cache, else rebuild and store."""

    @sf.single_flight("test:endpoint")
    async def handler(fresh: bool = False):
        if not fresh:
            cached = cache.get()
            if cached is not None:
                return cached
        await asyncio.sleep(rebuild_delay)
        rebuilds.append(1)
        body = f"body-{len(rebuilds)}"
        cache.set(body)
        return body

    return handler


def test_concurrent_misses_collapse_to_one_rebuild():
    """Five tabs hitting an expired TTL together cause one rebuild."""
    cache, rebuilds = _Cache(), []
    handler = _make_handler(cache, rebuilds)

    async def run():
        return await asyncio.gather(*(handler(fresh=False) for _ in range(5)))

    results = asyncio.run(run())

    assert len(rebuilds) == 1, f"expected one fan-out, got {len(rebuilds)}"
    # Every waiter gets the body, never None or an error.
    assert results == ["body-1"] * 5


def test_concurrent_forced_refreshes_still_collapse():
    """Concurrent forced refreshes also collapse to one rebuild."""
    cache, rebuilds = _Cache(), []
    handler = _make_handler(cache, rebuilds)

    async def run():
        return await asyncio.gather(*(handler(fresh=True) for _ in range(4)))

    results = asyncio.run(run())

    assert len(rebuilds) == 1
    assert results == ["body-1"] * 4


def test_a_lone_forced_refresh_still_bypasses_a_warm_cache():
    """A bypass must not be fobbed off with the cache it asked to skip.

    An unforced call must never mark itself as a completed rebuild, because it cannot
    tell a rebuild from a cache hit.
    """
    cache, rebuilds = _Cache(), []
    handler = _make_handler(cache, rebuilds)

    async def run():
        warm = await handler(fresh=False)          # populates the cache (one rebuild)
        cached = await handler(fresh=False)        # served from cache, must not mark
        forced = await handler(fresh=True)         # must rebuild anyway
        return warm, cached, forced

    warm, cached, forced = asyncio.run(run())

    assert warm == "body-1"
    assert cached == "body-1"
    assert forced == "body-2", "a forced refresh was served the cached body"
    assert len(rebuilds) == 2


def test_the_lock_registry_does_not_leak():
    """Locks are created per scope key and must be dropped once no caller holds them."""
    cache, rebuilds = _Cache(), []
    handler = _make_handler(cache, rebuilds, rebuild_delay=0)

    async def run():
        await asyncio.gather(*(handler(fresh=False) for _ in range(3)))

    asyncio.run(run())

    assert sf._locks == {}
    assert sf._waiters == {}
