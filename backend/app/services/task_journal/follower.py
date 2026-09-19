"""The journal follower: one worker of four follows every registered cluster's event stream.

Election is a Postgres session-level advisory lock on a dedicated connection. The lock dies
with the session, so a worker that is killed frees it without a word and the next retry on
another worker wins; a `SELECT 1` heartbeat every fifteen seconds is how a leader learns its
session is gone. The overlap between two leaders during a hand-over is bounded by one heartbeat
plus one retry, and every write the leader makes is idempotent, so the overlap is safe.

While leading: the registry is read every thirty seconds and every registered, non-excluded
cluster gets a firehose subscription on the events hub and a consumer; hub state frames set
the cluster's state row and schedule a seed; operation and lifecycle frames go to the writer;
and the sweeps (racing pairs, stale actions, reconcile, warnings) run on their own timers.

important: nothing here may name an org-scope helper. A background task's scope ContextVars
are unset, and unset means allowed; rows are written as facts and scope is applied on read.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from typing import Any, Awaitable, Callable, Dict, FrozenSet, Optional, Set

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.services.lxd import health, registry
from app.services.lxd.events_hub import get_events_hub
from app.services.task_journal import repository, seed
from app.services.task_journal.follower_frames import upstream_row
from app.services.task_journal.follower_writer import DashboardIdentities, JournalWriter
from app.services.task_journal.repository_types import FOLLOWER_LOCK

logger = logging.getLogger(__name__)

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"

HEARTBEAT_S = 15.0
REGISTRY_INTERVAL_S = 30.0
SWEEP_TICK_S = 5.0
MERGE_INTERVAL_S = 60.0
STALE_INTERVAL_S = 60.0
RECONCILE_INTERVAL_S = 120.0
WARNINGS_INTERVAL_S = 300.0
TOUCH_INTERVAL_S = 30.0
HUB_QUEUE_MAXSIZE = 2000

STATE_CANDIDATE = "candidate"
STATE_LEADER = "leader"
STATE_STEPPING_DOWN = "stepping_down"

ACTION_ELECTED = "task.journal.follower.elected"
ACTION_LOST = "task.journal.follower.lost"


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _default_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, poolclass=NullPool)


def excluded_clusters(raw: Optional[str] = None) -> FrozenSet[str]:
    """The lower-cased, trimmed ids of `TASK_JOURNAL_EXCLUDE_CLUSTERS`."""
    value = get_settings().task_journal_exclude_clusters if raw is None else raw
    return frozenset(part.strip().lower() for part in (value or "").split(",") if part.strip())


def _audit(action: str, **details: Any) -> None:
    try:
        from app.services.activity_logger import get_activity_logger

        get_activity_logger().log_activity(
            action=action,
            target_type="system",
            target_id="task_journal",
            details={"worker": WORKER_ID, **details},
        )
    except Exception:  # noqa: BLE001 - a failed audit line must not stop the follower
        logger.warning("Could not write the audit record for %s", action, exc_info=True)


class JournalFollower:
    def __init__(
        self,
        session_maker: Callable[[], Any],
        get_lxd_service: Callable[[], Any],
        *,
        retry_seconds: float,
        database_url: str,
        hub: Optional[Any] = None,
        engine_factory: Optional[Callable[[str], Any]] = None,
        identities: Optional[DashboardIdentities] = None,
        excluded: Optional[FrozenSet[str]] = None,
    ) -> None:
        self._session_maker = session_maker
        self._get_lxd_service = get_lxd_service
        self._retry_seconds = float(retry_seconds)
        self._database_url = database_url
        self._hub = hub or get_events_hub()
        self._engine_factory = engine_factory or _default_engine
        self._excluded = excluded_clusters() if excluded is None else excluded
        self._identities = identities or DashboardIdentities()
        self._writer = JournalWriter(
            session_maker, identities=self._identities, on_db_recovered=self._reseed_connected
        )
        self.state = STATE_CANDIDATE
        self._engine: Optional[Any] = None
        self._lock_conn: Optional[AsyncConnection] = None
        self._queues: Dict[str, asyncio.Queue] = {}
        self._consumers: Dict[str, asyncio.Task] = {}
        self._seeds: Dict[str, asyncio.Task] = {}
        self._cluster_state: Dict[str, str] = {}
        self._registry_task: Optional[asyncio.Task] = None
        self._sweep_task: Optional[asyncio.Task] = None
        self.elections = 0

    # ── election ──────────────────────────────────────────────────────────────────────────

    async def run(self) -> None:
        while True:
            try:
                if await self.try_acquire():
                    await self.lead()
            except asyncio.CancelledError:
                await self.step_down(graceful=True)
                raise
            except Exception:  # noqa: BLE001 - a leader that raised steps down and retries
                logger.error("Task journal follower iteration failed", exc_info=True)
                await self.step_down(graceful=False)
            await _sleep(self._retry_seconds)

    async def try_acquire(self) -> bool:
        engine = self._engine_factory(self._database_url)
        conn = await engine.connect()
        try:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            result = await conn.execute(
                text("SELECT pg_try_advisory_lock(:classid, :objid)"),
                {"classid": FOLLOWER_LOCK[0], "objid": FOLLOWER_LOCK[1]},
            )
            acquired = bool(result.scalar())
        except Exception:
            await self._close(conn, engine, unlock=False)
            raise
        if not acquired:
            await self._close(conn, engine, unlock=False)
            return False
        self._engine, self._lock_conn = engine, conn
        return True

    async def lead(self) -> None:
        self.state = STATE_LEADER
        self.elections += 1
        logger.info("Task journal follower elected on %s", WORKER_ID)
        async with self._session_maker() as db:
            await repository.reset_cluster_states(db)
            await db.commit()
        _audit(ACTION_ELECTED)
        self._writer.start()
        self._registry_task = asyncio.create_task(self._registry_loop(), name="task-journal-registry")
        self._sweep_task = asyncio.create_task(self._sweep_loop(), name="task-journal-sweeps")
        while True:
            await _sleep(HEARTBEAT_S)
            await self.heartbeat()

    async def heartbeat(self) -> None:
        # important: any exception means the session, and the lock with it, is gone.
        assert self._lock_conn is not None
        await self._lock_conn.execute(text("SELECT 1"))

    async def step_down(self, *, graceful: bool) -> None:
        if self.state == STATE_CANDIDATE and self._lock_conn is None:
            return
        was_leader = self.state == STATE_LEADER
        self.state = STATE_STEPPING_DOWN
        for task in (self._registry_task, self._sweep_task):
            await _cancel(task)
        self._registry_task = self._sweep_task = None
        for host in list(self._queues):
            await self._unfollow(host, record_state=False)
        await self._writer.stop()
        conn, engine = self._lock_conn, self._engine
        self._lock_conn = self._engine = None
        if conn is not None:
            await self._close(conn, engine, unlock=graceful)
        self._cluster_state.clear()
        if was_leader and not graceful:
            logger.warning("Task journal follower on %s lost its lock; standing by", WORKER_ID)
            _audit(ACTION_LOST)
        self.state = STATE_CANDIDATE

    async def _close(self, conn: Any, engine: Any, *, unlock: bool) -> None:
        if unlock:
            try:
                await conn.execute(
                    text("SELECT pg_advisory_unlock(:classid, :objid)"),
                    {"classid": FOLLOWER_LOCK[0], "objid": FOLLOWER_LOCK[1]},
                )
            except Exception:  # noqa: BLE001 - the session closing releases it anyway
                pass
        for closer in (getattr(conn, "close", None), getattr(engine, "dispose", None)):
            if closer is None:
                continue
            try:
                await closer()
            except Exception:  # noqa: BLE001
                pass

    # ── registry and consumers ────────────────────────────────────────────────────────────

    def is_excluded(self, host: str) -> bool:
        return host.strip().lower() in self._excluded

    async def _registry_loop(self) -> None:
        while True:
            try:
                await self.sync_registry()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.error("Task journal registry sync failed", exc_info=True)
            await _sleep(REGISTRY_INTERVAL_S)

    async def sync_registry(self) -> None:
        ids = await registry.registered_cluster_ids()
        if ids is None:
            return
        wanted: Set[str] = {host for host in ids if not self.is_excluded(host)}
        current = set(self._queues)
        for host in sorted(wanted - current):
            await self._follow(host)
        for host in sorted(current - wanted):
            await self._unfollow(host, record_state=True)

    async def _follow(self, host: str) -> None:
        queue = await self._hub.subscribe(host, firehose=True, maxsize=HUB_QUEUE_MAXSIZE)
        self._queues[host] = queue
        self._consumers[host] = asyncio.create_task(
            self._consume(host, queue), name=f"task-journal-consume-{host}"
        )
        logger.info("Task journal following %s", host)

    async def _unfollow(self, host: str, *, record_state: bool) -> None:
        await _cancel(self._consumers.pop(host, None))
        await _cancel(self._seeds.pop(host, None))
        queue = self._queues.pop(host, None)
        if queue is not None:
            await self._hub.unsubscribe(host, queue)
        self._cluster_state.pop(host, None)
        self._identities.forget(host)
        if record_state:
            async with self._session_maker() as db:
                await repository.set_cluster_state(db, host, "unregistered")
                await db.commit()
            logger.info("Task journal stopped following %s (unregistered); its rows are kept", host)

    async def _consume(self, host: str, queue: asyncio.Queue) -> None:
        last_touch = 0.0
        while True:
            frame = await queue.get()
            kind = frame.get("type")
            try:
                if kind == "hub":
                    await self.on_hub_state(host, frame)
                elif kind in ("operation", "lifecycle"):
                    await self._writer.submit(frame)
                    now = time.monotonic()
                    if now - last_touch >= TOUCH_INTERVAL_S:
                        last_touch = now
                        async with self._session_maker() as db:
                            await repository.touch_cluster_frame(db, host)
                            await db.commit()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad frame must not end the consumer
                logger.warning("Task journal consumer for %s failed on a frame", host, exc_info=True)

    async def on_hub_state(self, host: str, frame: dict) -> None:
        state = "connected" if frame.get("state") == "connected" else "disconnected"
        error = frame.get("error") if state == "disconnected" else None
        changed = self._cluster_state.get(host) != state
        self._cluster_state[host] = state
        async with self._session_maker() as db:
            await repository.set_cluster_state(db, host, state, error=error)
            if changed:
                await repository.insert_log_rows(db, [upstream_row(host, state, error)])
            await db.commit()
        if state == "connected":
            self.schedule_seed(host)

    def schedule_seed(self, host: str) -> None:
        previous = self._seeds.get(host)
        if previous is not None and not previous.done():
            return
        self._seeds[host] = asyncio.create_task(self._seed(host), name=f"task-journal-seed-{host}")

    async def _seed(self, host: str) -> None:
        try:
            await seed.seed_cluster(
                self._get_lxd_service(), host, self._session_maker, self._identities
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the next connect or reconcile tries again
            logger.warning("Seeding %s failed", host, exc_info=True)

    async def _reseed_connected(self) -> None:
        for host in self.connected_hosts():
            self.schedule_seed(host)

    def connected_hosts(self) -> list:
        return sorted(h for h, s in self._cluster_state.items() if s == "connected")

    # ── sweeps ────────────────────────────────────────────────────────────────────────────

    async def _sweep_loop(self) -> None:
        due = {
            "merge": 0.0, "stale": 0.0, "reconcile": RECONCILE_INTERVAL_S, "warnings": 0.0,
        }
        interval = {
            "merge": MERGE_INTERVAL_S,
            "stale": STALE_INTERVAL_S,
            "reconcile": RECONCILE_INTERVAL_S,
            "warnings": WARNINGS_INTERVAL_S,
        }
        started = time.monotonic()
        while True:
            await _sleep(SWEEP_TICK_S)
            elapsed = time.monotonic() - started
            for name, at in list(due.items()):
                if elapsed < at:
                    continue
                due[name] = elapsed + interval[name]
                try:
                    await self.run_sweep(name)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.error("Task journal sweep %s failed", name, exc_info=True)

    async def run_sweep(self, name: str) -> None:
        if name == "merge":
            await self._db_sweep(lambda db: repository.merge_racing_pairs(db))
        elif name == "stale":
            await self._db_sweep(lambda db: repository.expire_stale_actions(db))
        elif name == "reconcile":
            for host in self.connected_hosts():
                if health.is_tripped(host):
                    continue
                await seed.reconcile_cluster(
                    self._get_lxd_service(), host, self._session_maker, self._identities
                )
        elif name == "warnings":
            for host in self.connected_hosts():
                if health.is_tripped(host):
                    continue
                await seed.sweep_warnings(self._get_lxd_service(), host, self._session_maker)

    async def _db_sweep(self, fn: Callable[[Any], Awaitable[int]]) -> int:
        async with self._session_maker() as db:
            count = await fn(db)
            await db.commit()
        return count


async def _cancel(task: Optional[asyncio.Task]) -> None:
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


_follower: Optional[JournalFollower] = None
_task: Optional[asyncio.Task] = None


def start_follower(
    session_maker: Callable[[], Any],
    get_lxd_service: Callable[[], Any],
    *,
    retry_seconds: int,
    database_url: str,
) -> None:
    global _follower, _task
    if _task is not None and not _task.done():
        return
    _follower = JournalFollower(
        session_maker, get_lxd_service, retry_seconds=retry_seconds, database_url=database_url
    )
    _task = asyncio.create_task(_follower.run(), name="task-journal-follower")


async def stop_follower() -> None:
    global _follower, _task
    task = _task
    _task = None
    if task is not None:
        await _cancel(task)
    _follower = None


def get_follower() -> Optional[JournalFollower]:
    return _follower
