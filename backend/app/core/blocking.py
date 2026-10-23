"""
Bounded offload for blocking calls reached from an async path.

Three bounds are needed and they are not interchangeable:

  - a socket timeout on the call itself, which is the only thing that ever frees the worker
    thread;
  - a wall-clock cap, which bounds how long the caller waits;
  - a bounded pool, which bounds how much of the process one dead peer can pin.

`asyncio.wait_for` supplies only the second. Cancelling the await does NOT stop the thread,
so a call with no socket timeout leaks a worker permanently and the pool drains one request
at a time until nothing is left. That is precisely how the RC3 outage escalated from one
silent LXD cluster to a fully dead API.

This module is stdlib and logging only. It must never import from app.services or
app.routers, so any layer can use it without an import cycle.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Callable, Dict, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class BlockingTimeout(Exception):
    """A bounded blocking call exceeded its wall-clock budget.

    The caller stops waiting; the call does not stop. `pending` is the still-running call, so a
    write route can act when it really finishes (when_finished), such as clearing the list
    caches that a write landing after the 504 would otherwise leave stale for a whole TTL.
    """

    def __init__(
        self,
        label: str,
        timeout: float,
        pending: Optional["asyncio.Future[Any]"] = None,
    ) -> None:
        self.label = label
        self.timeout = timeout
        self.pending = pending
        super().__init__(f"{label} timed out after {timeout}s")

    def when_finished(self, callback: Callable[[], None]) -> None:
        """Run `callback` on the loop once the abandoned call ends, or now if that is unknown."""
        def _run(_future: "asyncio.Future[Any]") -> None:
            try:
                callback()
            except Exception:  # noqa: BLE001 - a follow-up must never surface in the loop
                logger.warning("follow-up after %s failed", self.label, exc_info=True)

        if self.pending is None or self.pending.done():
            _run(self.pending)  # type: ignore[arg-type]
        else:
            self.pending.add_done_callback(_run)


class BlockingPoolFull(Exception):
    """The pool is saturated. Shed load rather than grow an unbounded queue."""

    def __init__(self, label: str, in_flight_count: int) -> None:
        self.label = label
        self.in_flight_count = in_flight_count
        super().__init__(f"{label} rejected: {in_flight_count} calls already in flight")


_counter_lock = threading.Lock()
_in_flight: Dict[int, int] = {}
_in_flight_by_key: Dict[tuple[int, str], int] = {}


def in_flight(executor: ThreadPoolExecutor) -> int:
    """Tracked in-flight count for a pool. Zero when the pool is untracked."""
    with _counter_lock:
        return _in_flight.get(id(executor), 0)


def in_flight_for_key(executor: ThreadPoolExecutor, concurrency_key: str) -> int:
    """Tracked in-flight count for one bounded peer such as an LXD cluster."""
    with _counter_lock:
        return _in_flight_by_key.get((id(executor), concurrency_key), 0)


def _acquire(
    executor: ThreadPoolExecutor,
    max_in_flight: Optional[int],
    label: str,
    concurrency_key: Optional[str],
    max_in_flight_per_key: Optional[int],
) -> bool:
    if max_in_flight_per_key is not None and not concurrency_key:
        raise ValueError("concurrency_key is required with max_in_flight_per_key")
    if max_in_flight is None and max_in_flight_per_key is None:
        return False
    executor_key = id(executor)
    peer_key = (
        (executor_key, concurrency_key)
        if concurrency_key is not None and max_in_flight_per_key is not None
        else None
    )
    with _counter_lock:
        current = _in_flight.get(executor_key, 0)
        if max_in_flight is not None and current >= max_in_flight:
            raise BlockingPoolFull(label, current)
        peer_current = _in_flight_by_key.get(peer_key, 0) if peer_key is not None else 0
        if max_in_flight_per_key is not None and peer_current >= max_in_flight_per_key:
            raise BlockingPoolFull(label, peer_current)
        _in_flight[executor_key] = current + 1
        if peer_key is not None:
            _in_flight_by_key[peer_key] = peer_current + 1
    return True


def _release(executor: ThreadPoolExecutor, concurrency_key: Optional[str]) -> None:
    executor_key = id(executor)
    with _counter_lock:
        current = _in_flight.get(executor_key, 0)
        if current <= 1:
            _in_flight.pop(executor_key, None)
        else:
            _in_flight[executor_key] = current - 1
        if concurrency_key is not None:
            peer_key = (executor_key, concurrency_key)
            peer_current = _in_flight_by_key.get(peer_key, 0)
            if peer_current <= 1:
                _in_flight_by_key.pop(peer_key, None)
            else:
                _in_flight_by_key[peer_key] = peer_current - 1


def _on_done(
    executor: ThreadPoolExecutor, concurrency_key: Optional[str], future: Any
) -> None:
    # note: retrieve the exception so a call that failed after its wall-clock cap expired
    # does not surface later as "Future exception was never retrieved".
    try:
        if not future.cancelled():
            future.exception()
    except Exception:
        pass
    _release(executor, concurrency_key)


async def run_blocking(
    fn: Callable[..., T],
    *args: Any,
    timeout: float,
    label: str,
    executor: ThreadPoolExecutor,
    max_in_flight: Optional[int] = None,
    concurrency_key: Optional[str] = None,
    max_in_flight_per_key: Optional[int] = None,
    **kwargs: Any,
) -> T:
    """Run ``fn(*args, **kwargs)`` on ``executor`` and stop waiting after ``timeout`` seconds.

    important: on expiry the worker thread keeps running until its own socket timeout fires.
    Callers MUST give ``fn`` a per-attempt socket timeout, otherwise the thread is lost for
    good and this helper only hides the leak behind a prompt error.

    ``concurrency_key`` and ``max_in_flight_per_key`` add a second bound for one upstream
    peer. This prevents a single unavailable cluster from consuming the entire shared pool.

    Raises BlockingTimeout on expiry and BlockingPoolFull when either bound is saturated.
    Any exception raised by ``fn`` propagates unchanged.
    """
    tracked = _acquire(
        executor,
        max_in_flight,
        label,
        concurrency_key,
        max_in_flight_per_key,
    )
    loop = asyncio.get_running_loop()
    started = time.monotonic()

    # important: carry the caller's context into the worker. `loop.run_in_executor` does not,
    # so every ContextVar read in the thread returned its DEFAULT, and this codebase runs two
    # semantics side by side: `asyncio.to_thread` copies the context (it wraps the call in
    # `ctx.run` itself) and this did not. Three things depended on the difference and none of
    # them announced it.
    #
    # The org-scope filter at the top of connection.get_client reads `_allowed_clusters_var`,
    # which defaults to None meaning "no filter", so the filter could never deny anything
    # reached through here. services/lxd/status.py already records that and warns that a future
    # caller which does not pre-filter its own list gets no second line of defence.
    #
    # `org_scope.get_scope_cache_key` returns ":scope=all" on a None var, which is the ADMIN
    # cache namespace. Every current call site runs on the loop before the offload, so nothing
    # is poisoned today, and this removes the way it could be.
    #
    # `org_scope.check_tenant_permission` returns True on a None ACL. In a worker that is
    # unconditional, so it fails OPEN. No target of this helper calls it today.
    #
    # It also restores the request id and the actor to every log line emitted from a worker,
    # since core/request_context.py is a ContextVar too.
    ctx = contextvars.copy_context()
    call = (
        partial(ctx.run, fn, *args, **kwargs)
        if kwargs
        else partial(ctx.run, fn, *args)
    )
    try:
        future = loop.run_in_executor(executor, call)
    except Exception:
        if tracked:
            _release(executor, concurrency_key)
        raise

    if tracked:
        # note: shield below keeps `future` pending until the thread really finishes, so this
        # callback fires then and not at cancellation time. That is what keeps the in-flight
        # count honest about how much of the pool a dead peer is holding.
        future.add_done_callback(lambda f: _on_done(executor, concurrency_key, f))

    try:
        return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(
            "blocking call %s exceeded %.1fs; the worker thread runs on until its socket "
            "timeout fires (in_flight=%d)",
            label,
            timeout,
            in_flight(executor),
        )
        raise BlockingTimeout(label, timeout, pending=future) from None
    except Exception:
        logger.debug(
            "blocking call %s failed after %.2fs", label, time.monotonic() - started
        )
        raise
