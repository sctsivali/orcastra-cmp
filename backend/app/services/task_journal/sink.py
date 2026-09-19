"""The activity sink: every worker's copy of its audit records into the task journal.

`log_activity` is reached from request handlers, from worker threads under `run_blocking`, and
from background tasks with no request at all. It offers each record here and returns; one
writer task per worker drains the queue in batches, one transaction per batch. The stdout line
the logger already wrote is the audit of record, so nothing in this module may raise into the
caller, and a batch the database refuses three times is dropped with an error line rather than
held.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.core.config import get_settings
from app.services.task_journal import repository
from app.services.task_journal.sink_mapping import (
    MODE_ATTACH_BY_ID,
    MODE_UPLOAD_CLOSE,
    MODE_UPLOAD_OPEN,
    TaskWrite,
    record_to_log_row,
    record_to_task,
)

logger = logging.getLogger(__name__)

QUEUE_MAX = 5000
BATCH_MAX = 200
BATCH_WINDOW_S = 0.25
RETRY_DELAYS_S = (0.5, 1.0, 2.0)
DROP_WARNING_INTERVAL_S = 60.0
STOP_DRAIN_S = 5.0
ORG_MEMO_TTL_S = 60.0


class JournalSink:
    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: Optional[asyncio.Queue] = None
        self._task: Optional[asyncio.Task] = None
        self._session_maker: Optional[Callable[[], Any]] = None
        self._started = False
        self._last_drop_warning = 0.0
        self._org_memo: Dict[str, Tuple[float, Tuple[Optional[int], Optional[str]]]] = {}
        self.dropped = 0
        self.written = 0

    def start(
        self, loop: asyncio.AbstractEventLoop, session_maker: Optional[Callable[[], Any]] = None
    ) -> None:
        if self._started:
            return
        if session_maker is None:
            from app.core.database import async_session_maker

            session_maker = async_session_maker
        self._loop = loop
        self._session_maker = session_maker
        self._queue = asyncio.Queue(maxsize=QUEUE_MAX)
        self._task = loop.create_task(self._writer(), name="task-journal-sink")
        self._started = True

    async def stop(self) -> None:
        self._started = False
        queue, task = self._queue, self._task
        if queue is not None:
            deadline = time.monotonic() + STOP_DRAIN_S
            while not queue.empty() and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown
                pass
        self._task = None
        self._queue = None
        self._loop = None

    def offer(self, record: Dict[str, Any]) -> None:
        # important: sync and total. It is called from worker threads, so the record crosses
        # to the loop with call_soon_threadsafe; a closed loop, a stopped sink or a disabled
        # journal all mean "return", never "raise".
        try:
            if not self._started or self._loop is None or self._queue is None:
                return
            if not get_settings().task_journal_enabled:
                return
            self._loop.call_soon_threadsafe(self._enqueue, record)
        except Exception:  # noqa: BLE001 - the stdout record is the audit of record
            return

    def _enqueue(self, record: Dict[str, Any]) -> None:
        queue = self._queue
        if queue is None:
            return
        try:
            queue.put_nowait(record)
        except asyncio.QueueFull:
            self.dropped += 1
            now = time.monotonic()
            if now - self._last_drop_warning >= DROP_WARNING_INTERVAL_S:
                self._last_drop_warning = now
                logger.warning(
                    "Task journal queue is full; %d audit records dropped so far "
                    "(the stdout record is the audit of record)",
                    self.dropped,
                )

    async def _next_batch(self) -> List[Dict[str, Any]]:
        assert self._queue is not None
        batch = [await self._queue.get()]
        deadline = time.monotonic() + BATCH_WINDOW_S
        while len(batch) < BATCH_MAX:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                batch.append(await asyncio.wait_for(self._queue.get(), remaining))
            except asyncio.TimeoutError:
                break
        return batch

    async def _writer(self) -> None:
        # The scheduler_loop shape: one iteration can fail however it likes and the next still
        # runs, so a poison record or a database outage never stops the drain.
        while True:
            try:
                batch = await self._next_batch()
                await self._write_with_retry(batch)
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                logger.error("Task journal writer iteration failed", exc_info=True)
                try:
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    break

    async def _write_with_retry(self, batch: List[Dict[str, Any]]) -> None:
        # One attempt, then three retries at 0.5, 1 and 2 seconds; then the batch is dropped.
        attempts = len(RETRY_DELAYS_S) + 1
        for attempt in range(attempts):
            try:
                await self._write_batch(batch)
                return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                if attempt < len(RETRY_DELAYS_S):
                    delay = RETRY_DELAYS_S[attempt]
                    logger.warning(
                        "Task journal batch write attempt %d/%d failed, retrying in %.1fs",
                        attempt + 1, attempts, delay, exc_info=True,
                    )
                    await asyncio.sleep(delay)
        logger.error(
            "%d audit records not journaled; the stdout record is the audit of record",
            len(batch),
        )

    async def _write_batch(self, batch: List[Dict[str, Any]]) -> None:
        assert self._session_maker is not None
        mapped: List[Tuple[Optional[Dict[str, Any]], Optional[TaskWrite]]] = []
        for record in batch:
            try:
                mapped.append((record_to_log_row(record), record_to_task(record)))
            except Exception:  # noqa: BLE001 - one bad record must not hold the batch
                logger.warning("Audit record could not be mapped for the journal", exc_info=True)
        if not any(log_row or task for log_row, task in mapped):
            return
        async with self._session_maker() as db:
            log_rows: List[Dict[str, Any]] = []
            for log_row, task in mapped:
                task_id = await self._apply_task(db, task) if task else None
                if log_row is not None:
                    if task_id:
                        log_row["task_id"] = task_id
                    await self._backfill_org(db, log_row)
                    log_rows.append(log_row)
            await repository.insert_log_rows(db, log_rows)
            await db.commit()
            self.written += len(log_rows)

    async def _apply_task(self, db: Any, task: TaskWrite) -> Optional[str]:
        actor = task.actor
        if actor.target_organization_id is None:
            org_id, slug = await self._org_for(db, task.key.cluster_id)
            if org_id is not None:
                actor = dataclasses.replace(
                    actor, target_organization_id=org_id, target_organization_slug=slug
                )
        if task.mode == MODE_ATTACH_BY_ID and task.operation_id:
            return await repository.attach_operation_by_id(
                db,
                cluster_id=task.key.cluster_id,
                operation_id=task.operation_id,
                kind=task.kind,
                key=task.key,
                actor=actor,
                result=task.result,
                description=task.description,
                audit_error=task.audit_error,
                extra=task.extra,
            )
        if task.mode == MODE_UPLOAD_CLOSE and task.session_key:
            closed = await repository.update_action_by_session(
                db, session_key=task.session_key, result=task.result, audit_error=task.audit_error
            )
            if closed:
                return closed
        if task.mode not in (MODE_UPLOAD_OPEN, MODE_UPLOAD_CLOSE):
            claimed = await repository.claim_operation_for_audit(
                db, key=task.key, actor=actor, result=task.result,
                audit_error=task.audit_error, extra=task.extra,
            )
            if claimed:
                return claimed
        return await repository.insert_action(
            db,
            kind=task.kind,
            key=task.key,
            actor=actor,
            result=task.result,
            duration_ms=task.duration_ms,
            description=task.description,
            audit_error=task.audit_error,
            extra=task.extra,
            session_key=task.session_key,
        )

    async def _backfill_org(self, db: Any, log_row: Dict[str, Any]) -> None:
        if log_row.get("target_organization_id") is not None or not log_row.get("cluster_id"):
            return
        org_id, _slug = await self._org_for(db, str(log_row["cluster_id"]))
        if org_id is not None:
            log_row["target_organization_id"] = org_id

    async def _org_for(self, db: Any, cluster_id: str) -> Tuple[Optional[int], Optional[str]]:
        # note: a DB label lookup memoised per worker, not a scope helper; it names the org a
        # cluster is bound to so a Partner's own rows carry their org without a join on read.
        now = time.monotonic()
        cached = self._org_memo.get(cluster_id)
        if cached and cached[0] > now:
            return cached[1]
        from app.core.org_scope import resolve_cluster_org

        try:
            resolved = await resolve_cluster_org(cluster_id, db)
        except Exception:  # noqa: BLE001 - an unknown cluster is a row without an org
            resolved = (None, None)
        self._org_memo[cluster_id] = (now + ORG_MEMO_TTL_S, resolved)
        return resolved


_sink: Optional[JournalSink] = None


def get_journal_sink() -> JournalSink:
    global _sink
    if _sink is None:
        _sink = JournalSink()
    return _sink
