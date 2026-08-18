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
from typing import Dict


_locks: Dict[str, asyncio.Lock] = {}
_waiters: Dict[str, int] = {}


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
