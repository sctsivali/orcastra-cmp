"""The per-worker feed behind `/tasks/ws`: tails the journal tables and fans deltas out.

Postgres is the only source of truth (the follower may live on another worker, the sink writes
from every worker), so one task per worker polls the tables once a second while a subscriber
exists and stops on the last unsubscribe, like the events hub. Ordering is by `seq` (bumped on
every task change) and by `id` for log lines. The watermark is the database clock read in the
same transaction as the rows; every tick looks back five seconds with a bounded dedupe, because a
row from a transaction that started before the previous watermark can commit after it.

This module names no scope helper: each subscriber's predicate is judged under its captured
context before a frame is queued, so a frame a caller may not see never enters its queue.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Mapping, Optional

from sqlalchemy import text

from app.services.task_journal import repository

logger = logging.getLogger(__name__)

TICK_S = 1.0
LOOKBACK_S = 5
PAGE = 500
QUEUE_MAXSIZE = 500
DEDUPE_CAPACITY = 10_000
FOLLOWER_CHECK_S = 15.0

_TASK_PAGE = text(
    "SELECT * FROM task_journal WHERE updated_at > :since ORDER BY seq LIMIT :page"
)
_LOG_PAGE = text("SELECT * FROM cluster_log WHERE ts > :since ORDER BY id LIMIT :page")
_NOW = text("SELECT now()")

Accept = Callable[[Dict[str, Any]], bool]


@dataclass
class _Subscriber:
    queue: asyncio.Queue
    accept: Accept
    ctx: contextvars.Context
    accept_failed: bool = False


@dataclass
class _Lru:
    capacity: int
    seen: "OrderedDict[Any, None]" = field(default_factory=OrderedDict)

    def add(self, key: Any) -> bool:
        """True when `key` is new."""
        if key in self.seen:
            self.seen.move_to_end(key)
            return False
        self.seen[key] = None
        while len(self.seen) > self.capacity:
            self.seen.popitem(last=False)
        return True


class TaskFeedTailer:
    """One per worker; `get_task_feed_tailer()` hands out the singleton."""

    def __init__(self, session_maker: Optional[Callable[[], Any]] = None, *, run_loop: bool = True):
        self._session_maker = session_maker
        self._run_loop = run_loop
        self._subscribers: Dict[asyncio.Queue, _Subscriber] = {}
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._watermark: Optional[datetime] = None
        self._seen_tasks = _Lru(DEDUPE_CAPACITY)
        self._seen_logs = _Lru(DEDUPE_CAPACITY)
        self._follower_active: Optional[bool] = None
        self._follower_checked_at: float = 0.0

    # -- subscriptions ---------------------------------------------------------------------

    async def subscribe(self, accept: Accept) -> asyncio.Queue:
        """Register a subscriber; its frames are judged under the context captured NOW."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        async with self._lock:
            self._subscribers[queue] = _Subscriber(
                queue=queue, accept=accept, ctx=contextvars.copy_context()
            )
            if self._run_loop and (self._task is None or self._task.done()):
                self._task = asyncio.create_task(self._run())
        return queue

    def rescope(self, queue: asyncio.Queue) -> None:
        """Replace the context a subscriber's frames are judged under (called from its task)."""
        sub = self._subscribers.get(queue)
        if sub is not None:
            sub.ctx = contextvars.copy_context()

    async def unsubscribe(self, queue: asyncio.Queue) -> None:
        async with self._lock:
            self._subscribers.pop(queue, None)
            if not self._subscribers and self._task is not None and not self._task.done():
                self._task.cancel()
                self._task = None

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def follower_state(self) -> Optional[bool]:
        """The last follower reading, or None before the first tick."""
        return self._follower_active

    # -- the loop ----------------------------------------------------------------------------

    def _sessions(self) -> Callable[[], Any]:
        if self._session_maker is None:
            from app.core.database import async_session_maker

            self._session_maker = async_session_maker
        return self._session_maker

    async def _run(self) -> None:
        try:
            while self._subscribers:
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("task feed tick failed: %s", exc)
                await asyncio.sleep(TICK_S)
        except asyncio.CancelledError:
            pass
        finally:
            self._watermark = None

    async def tick(self) -> None:
        """One poll: read the clock and both pages in one transaction, then fan out."""
        tasks: List[Mapping[str, Any]] = []
        logs: List[Mapping[str, Any]] = []
        follower: Optional[bool] = None
        check_follower = time.monotonic() - self._follower_checked_at >= FOLLOWER_CHECK_S
        async with self._sessions()() as db:
            now = (await db.execute(_NOW)).scalar()
            if self._watermark is not None:
                since = self._watermark - timedelta(seconds=LOOKBACK_S)
                params = {"since": since, "page": PAGE}
                tasks = list((await db.execute(_TASK_PAGE, params)).mappings().all())
                logs = list((await db.execute(_LOG_PAGE, params)).mappings().all())
            if check_follower or self._follower_active is None:
                follower = await repository.follower_is_active(db)
        # The first tick only sets the watermark, so a reconnecting tab does not replay history
        # it refetches on `hello` anyway.
        first = self._watermark is None
        self._watermark = now
        if follower is not None:
            self._follower_checked_at = time.monotonic()
            changed = self._follower_active is not None and follower != self._follower_active
            self._follower_active = follower
            if changed:
                self._fanout({"type": "follower", "active": follower})
        if first:
            return
        for row in tasks:
            if self._seen_tasks.add((str(row["id"]), int(row["seq"]))):
                self._fanout({"type": "task", "row": row})
        for row in logs:
            if self._seen_logs.add(int(row["id"])):
                self._fanout({"type": "log", "row": row})
        if len(tasks) >= PAGE or len(logs) >= PAGE:
            # A full page means the tick may have fallen behind the writers; the client
            # refetches its lists rather than trusting a stream with a hole in it.
            self._fanout({"type": "resync", "reason": "lag"})

    def _fanout(self, frame: Dict[str, Any]) -> None:
        for sub in list(self._subscribers.values()):
            try:
                allowed = sub.ctx.run(sub.accept, frame)
            except Exception:
                # Fail closed; one bad predicate must not stop the feed for the other
                # subscribers on this worker.
                if not sub.accept_failed:
                    sub.accept_failed = True
                    logger.exception("task feed subscriber predicate raised; its frames are dropped")
                continue
            if not allowed:
                continue
            try:
                sub.queue.put_nowait(frame)
            except asyncio.QueueFull:
                # Slow consumer: drop the oldest frame to keep the feed fresh.
                try:
                    sub.queue.get_nowait()
                    sub.queue.put_nowait(frame)
                except Exception:
                    pass


_tailer: Optional[TaskFeedTailer] = None


def get_task_feed_tailer() -> TaskFeedTailer:
    global _tailer
    if _tailer is None:
        _tailer = TaskFeedTailer()
    return _tailer
