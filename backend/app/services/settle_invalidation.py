"""Clear the dashboard's list caches again once an accepted LXD operation has finished.

A write LXD answers with 202 has not happened yet. The route clears the caches when it accepts
the write, but a list read in the seconds that follow still sees the pre-write state and caches
it for that list's TTL: the instance a proxy caller just deleted is listed again, the snapshot a
`dir` pool is still copying never appears. One bounded waiter per accepted operation clears the
same caches a second time at the moment the cluster reports the operation settled.

Best effort and bounded: a waiter never holds the request, never raises, and gives up after
SETTLE_DEADLINE_SECONDS. Past MAX_WAITERS per worker a write relies on its cache TTL and says so
in the log.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Awaitable, Callable, Dict, Optional, Set

from app.services.lxd.snapshots import wait_for_operation

logger = logging.getLogger(__name__)

MAX_WAITERS = 32
# Per owner (one API key, or the dashboard's own routes), so one integration starting long
# operations cannot take every slot from the others.
MAX_WAITERS_PER_BUCKET = 8
SETTLE_DEADLINE_SECONDS = 600
# One LXD wait call per slice, on this module's own threads, so a long operation never holds a
# thread for minutes and never queues behind (or in front of) request work.
WAIT_SLICE_SECONDS = 10
MIN_POLL_SECONDS = 1.0

_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="settle_")
_waiters: Set[asyncio.Task] = set()
_by_bucket: Dict[str, Set[asyncio.Task]] = {}


def invalidate_when_settled(
    get_client: Callable[[], Any],
    operation_id: Optional[str],
    project: Optional[str],
    invalidate: Callable[[], None],
    *,
    label: str,
    bucket: str = "dashboard",
    on_outcome: Optional[Callable[[str], Awaitable[None]]] = None,
) -> bool:
    """Run `invalidate` once the operation settles. Returns False when no waiter was started.

    `get_client` returns the cluster's pylxd client; it runs on this module's threads because
    acquiring a pooled client can revalidate the connection.

    `on_outcome` receives how the wait ended: LXD's status ("Success", "Failure", ...) when the
    operation settled, or a description of why it is not known. It is awaited after the caches are
    cleared and must act only on "Success" (an instance's access rules, services/instance_grants.py).
    """
    if not operation_id:
        return False
    owned = _by_bucket.setdefault(bucket, set())
    if len(_waiters) >= MAX_WAITERS or len(owned) >= MAX_WAITERS_PER_BUCKET:
        logger.warning("settle waiter limit reached; %s relies on its cache TTL", label)
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    task = loop.create_task(_settle(get_client, operation_id, project, invalidate, label, on_outcome))
    _waiters.add(task)
    owned.add(task)

    def _forget(done: asyncio.Task) -> None:
        _waiters.discard(done)
        owned.discard(done)
        if not owned:
            _by_bucket.pop(bucket, None)

    task.add_done_callback(_forget)
    return True


async def _settle(
    get_client: Callable[[], Any],
    operation_id: str,
    project: Optional[str],
    invalidate: Callable[[], None],
    label: str,
    on_outcome: Optional[Callable[[str], Awaitable[None]]] = None,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SETTLE_DEADLINE_SECONDS
    outcome = "deadline"
    try:
        try:
            # the caller's org scope travels with the task; the pooled client honours it
            client = await loop.run_in_executor(
                _executor, partial(contextvars.copy_context().run, get_client)
            )
        except Exception as exc:  # noqa: BLE001 - nothing to wait on; clear and stop
            client = None
            outcome = f"no client ({type(exc).__name__})"
        if client is None:
            outcome = outcome if outcome != "deadline" else "no client"
            return
        while loop.time() < deadline:
            try:
                state = await loop.run_in_executor(
                    _executor,
                    partial(
                        contextvars.copy_context().run,
                        wait_for_operation,
                        client,
                        operation_id,
                        project or "default",
                        WAIT_SLICE_SECONDS,
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - a vanished operation has finished
                # LXD reaps a finished operation after a while and then answers 404; a cluster
                # that stopped answering is no reason to keep a waiter either. Either way the
                # clearance below is the safe end.
                outcome = f"unreadable ({type(exc).__name__})"
                break
            if state.get("settled"):
                outcome = str(state.get("status"))
                break
            # LXD normally holds a wait for the whole slice; one that answers at once must not
            # turn this into a busy loop against the cluster
            await asyncio.sleep(MIN_POLL_SECONDS)
    finally:
        try:
            invalidate()
        except Exception:  # noqa: BLE001 - the TTL still bounds the stale window
            logger.warning("settle invalidation failed for %s", label, exc_info=True)
        logger.info("settle %s %s: caches cleared (%s)", label, operation_id, outcome)
        if on_outcome is not None:
            try:
                await on_outcome(outcome)
            except Exception:  # noqa: BLE001 - the follow-up reports its own failure
                logger.warning("settle follow-up failed for %s", label, exc_info=True)


async def shutdown() -> None:
    """Cancel waiters still running when the worker stops."""
    tasks = list(_waiters)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _waiters.clear()
    _by_bucket.clear()
