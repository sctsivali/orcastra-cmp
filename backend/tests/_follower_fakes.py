"""Hand-rolled fakes for the journal follower tests: a lock store two followers race on, an
events hub that records subscriptions, and a sleep that a test scripts by duration.

No database, daemon or network; tests assert on the calls these fakes record.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

from tests._journal_fakes import FakeResult


class LockStore:
    """The advisory lock as Postgres keeps it: held by one session until unlocked or closed."""

    def __init__(self) -> None:
        self.holder: Optional["FakeLockConn"] = None
        self.attempts = 0
        self.connections: List["FakeLockConn"] = []


class FakeLockConn:
    def __init__(self, store: LockStore, *, fail_heartbeat_after: Optional[int] = None) -> None:
        self.store = store
        self.executed: List[Tuple[str, Any]] = []
        self.options: Dict[str, Any] = {}
        self.closed = False
        self.heartbeats = 0
        self.fail_heartbeat_after = fail_heartbeat_after
        store.connections.append(self)

    async def execution_options(self, **options: Any) -> "FakeLockConn":
        self.options.update(options)
        return self

    async def execute(self, statement: Any, params: Any = None) -> FakeResult:
        sql = str(statement)
        self.executed.append((sql, params))
        if "pg_try_advisory_lock" in sql:
            self.store.attempts += 1
            if self.store.holder is None:
                self.store.holder = self
            return FakeResult(scalar=self.store.holder is self)
        if "pg_advisory_unlock" in sql:
            if self.store.holder is self:
                self.store.holder = None
            return FakeResult(scalar=True)
        if sql.strip() == "SELECT 1":
            self.heartbeats += 1
            if self.fail_heartbeat_after is not None and self.heartbeats > self.fail_heartbeat_after:
                # The session is gone; Postgres freed the lock with it.
                if self.store.holder is self:
                    self.store.holder = None
                raise ConnectionError("server closed the connection unexpectedly")
            return FakeResult(scalar=1)
        return FakeResult()

    async def close(self) -> None:
        self.closed = True
        if self.store.holder is self:
            self.store.holder = None


class FakeEngine:
    def __init__(self, store: LockStore, *, fail_heartbeat_after: Optional[int] = None) -> None:
        self.store = store
        self.fail_heartbeat_after = fail_heartbeat_after
        self.disposed = 0
        self.urls: List[str] = []

    def factory(self, url: str) -> "FakeEngine":
        self.urls.append(url)
        return self

    async def connect(self) -> FakeLockConn:
        return FakeLockConn(self.store, fail_heartbeat_after=self.fail_heartbeat_after)

    async def dispose(self) -> None:
        self.disposed += 1


class FakeHub:
    """Records every subscribe and unsubscribe; hands out real queues a test can feed."""

    def __init__(self) -> None:
        self.subscribed: List[Tuple[str, bool, int]] = []
        self.unsubscribed: List[str] = []
        self.queues: Dict[str, asyncio.Queue] = {}

    async def subscribe(
        self, host: str, accept: Any = None, *, firehose: bool = False, maxsize: int = 100
    ) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.subscribed.append((host, firehose, maxsize))
        self.queues[host] = queue
        return queue

    async def unsubscribe(self, host: str, queue: asyncio.Queue) -> None:
        self.unsubscribed.append(host)
        self.queues.pop(host, None)


class ScriptedSleep:
    """`_sleep` replaced by duration: some return at once, some park the caller for good, and
    one duration ends the test by cancelling after N calls."""

    def __init__(
        self,
        *,
        instant: Tuple[float, ...] = (),
        park: Tuple[float, ...] = (),
        stop_on: Optional[float] = None,
        stop_after: int = 1,
    ) -> None:
        self.calls: List[float] = []
        self.instant = set(instant)
        self.park = set(park)
        self.stop_on = stop_on
        self.stop_after = stop_after
        self.stopped = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self.stop_on is not None and seconds == self.stop_on:
            if self.calls.count(seconds) >= self.stop_after:
                self.stopped.set()
                raise asyncio.CancelledError()
        if seconds in self.park:
            await asyncio.Event().wait()
        # Every other duration yields once so the other tasks make progress.
        await asyncio.sleep(0)


class NoIdentities:
    """No certificate read: every host has no dashboard identity."""

    def __init__(self, **_: object) -> None:
        # The follower builds its default with read_tracked=...; the fake ignores it.
        pass

    async def for_host(self, host: str) -> frozenset:
        return frozenset()

    def forget(self, host: str) -> None:
        return None
