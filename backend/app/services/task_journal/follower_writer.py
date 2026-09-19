"""The follower's writer: one inbox, one transaction per batch, progress coalesced per operation.

Every per-host consumer offers its operation and lifecycle frames here. The writer drains the
inbox in batches of up to 200 frames or 250 ms, holds progress-only frames so one operation
costs at most one write per second however fast the daemon reports, flushes a status change at
once, and retries a refused batch with backoff while holding it. Once the database answers
again after a refusal, the owner is told so every connected cluster is seeded to repair what
the outage hid.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from app.services.task_journal import repository
from app.services.task_journal.follower_frames import (
    is_progress_only,
    lifecycle_row,
    operation_frame,
)
from app.services.task_journal.vocabulary import TERMINAL, status_from_lxd

logger = logging.getLogger(__name__)

INBOX_MAX = 10000
BATCH_MAX = 200
BATCH_WINDOW_S = 0.25
PROGRESS_INTERVAL_S = 1.0
RETRY_DELAYS_S = (0.5, 1.0, 2.0, 4.0, 8.0)
RETRY_ATTEMPTS = 10
STOP_DRAIN_S = 5.0
IDENTITY_RETRY_S = 60.0
_STATUS_MEMO_MAX = 10000

OpKey = Tuple[str, str]


def _der_from_pem(pem: str) -> bytes:
    body = "".join(
        line.strip()
        for line in pem.splitlines()
        if line.strip() and not line.startswith("-----")
    )
    return base64.b64decode(body)


def fingerprint(pem: str) -> str:
    """The lower-cased sha256 hex of the certificate's DER bytes, which is how LXD names a TLS
    client in a `requestor` block."""
    return hashlib.sha256(_der_from_pem(pem)).hexdigest().lower()


class DashboardIdentities:
    """Per-cluster set of the identities that are this dashboard, computed once off the loop."""

    def __init__(self, read_certificate: Optional[Callable[[str], Dict[str, str]]] = None):
        self._read = read_certificate or _read_cluster_certificate
        self._known: Dict[str, frozenset] = {}
        self._failed_at: Dict[str, float] = {}

    async def for_host(self, host: str) -> frozenset:
        cached = self._known.get(host)
        if cached is not None:
            return cached
        failed = self._failed_at.get(host)
        if failed is not None and time.monotonic() - failed < IDENTITY_RETRY_S:
            return frozenset()
        try:
            identities = await asyncio.to_thread(self._compute, host)
        except Exception as exc:  # noqa: BLE001 - a missing cert only blurs the actor kind
            self._failed_at[host] = time.monotonic()
            logger.warning("Could not fingerprint the client certificate for %s: %s", host, exc)
            return frozenset()
        self._known[host] = identities
        return identities

    def forget(self, host: str) -> None:
        self._known.pop(host, None)
        self._failed_at.pop(host, None)

    def _compute(self, host: str) -> frozenset:
        cert = self._read(host) or {}
        pem = cert.get("client.crt")
        if not pem:
            return frozenset()
        return frozenset({fingerprint(pem)})


def _read_cluster_certificate(host: str) -> Dict[str, str]:
    from app.services.terminal_service import get_terminal_service

    return get_terminal_service().cert_manager.get_cluster_certificate(host)


class JournalWriter:
    def __init__(
        self,
        session_maker: Callable[[], Any],
        *,
        identities: DashboardIdentities,
        on_db_recovered: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        self._session_maker = session_maker
        self._identities = identities
        self._on_db_recovered = on_db_recovered
        self._inbox: asyncio.Queue = asyncio.Queue(maxsize=INBOX_MAX)
        self._task: Optional[asyncio.Task] = None
        self._pending_progress: Dict[OpKey, dict] = {}
        self._progress_written_at: Dict[OpKey, float] = {}
        self._last_status: Dict[OpKey, str] = {}
        self._degraded = False
        self.dropped_progress = 0
        self.written = 0

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="task-journal-follower-writer")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        deadline = time.monotonic() + STOP_DRAIN_S
        while not self._inbox.empty() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown
            pass
        self._task = None

    async def submit(self, frame: dict) -> None:
        """Offer one hub frame. A progress-only frame is dropped when the inbox is full; any
        other frame waits for a slot, so the hub's own queue absorbs the burst."""
        if self._inbox.full() and is_progress_only(frame):
            self.dropped_progress += 1
            return
        await self._inbox.put(frame)

    def submit_nowait(self, frame: dict) -> bool:
        try:
            self._inbox.put_nowait(frame)
        except asyncio.QueueFull:
            return False
        return True

    def _next_progress_due_in(self) -> Optional[float]:
        if not self._pending_progress:
            return None
        now = time.monotonic()
        earliest = min(
            self._progress_written_at.get(key, 0.0) + PROGRESS_INTERVAL_S
            for key in self._pending_progress
        )
        return max(0.0, earliest - now)

    async def _next_batch(self) -> List[dict]:
        due_in = self._next_progress_due_in()
        try:
            first = await asyncio.wait_for(self._inbox.get(), due_in)
        except asyncio.TimeoutError:
            return []
        batch = [first]
        deadline = time.monotonic() + BATCH_WINDOW_S
        while len(batch) < BATCH_MAX:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                batch.append(await asyncio.wait_for(self._inbox.get(), remaining))
            except asyncio.TimeoutError:
                break
        return batch

    @staticmethod
    def _op_key(frame: dict) -> OpKey:
        return (str(frame.get("host") or ""), str(frame.get("operation_id") or ""))

    def _remember_status(self, key: OpKey, status: str) -> None:
        if status in TERMINAL:
            self._last_status.pop(key, None)
            self._progress_written_at.pop(key, None)
            return
        self._last_status[key] = status
        while len(self._last_status) > _STATUS_MEMO_MAX:
            self._last_status.pop(next(iter(self._last_status)))

    def coalesce(self, batch: List[dict]) -> List[dict]:
        """Which frames to write now.

        A progress-only frame whose operation was written less than a second ago is held (the
        newest replaces the older one). Anything that changes the status is written at once and
        clears whatever was held for that operation. Held frames whose second has passed are
        appended.
        """
        now = time.monotonic()
        out: List[dict] = []
        for frame in batch:
            if frame.get("type") != "operation":
                out.append(frame)
                continue
            key = self._op_key(frame)
            status = status_from_lxd(frame.get("status"), frame.get("status_code"))
            status_changed = self._last_status.get(key) != status
            if is_progress_only(frame) and not status_changed:
                last = self._progress_written_at.get(key, 0.0)
                if now - last < PROGRESS_INTERVAL_S:
                    self._pending_progress[key] = frame
                    continue
            self._pending_progress.pop(key, None)
            self._progress_written_at[key] = now
            self._remember_status(key, status)
            out.append(frame)
        for key in list(self._pending_progress):
            if now - self._progress_written_at.get(key, 0.0) >= PROGRESS_INTERVAL_S:
                out.append(self._pending_progress.pop(key))
                self._progress_written_at[key] = now
        return out

    async def _loop(self) -> None:
        # The scheduler_loop shape: one iteration may fail however it likes and the next runs.
        while True:
            try:
                batch = await self._next_batch()
                frames = self.coalesce(batch)
                if frames:
                    await self.write_with_retry(frames)
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                logger.error("Task journal follower writer iteration failed", exc_info=True)
                try:
                    await asyncio.sleep(0.5)
                except asyncio.CancelledError:
                    break

    async def write_with_retry(self, frames: List[dict]) -> bool:
        for attempt in range(RETRY_ATTEMPTS):
            try:
                await self.write_batch(frames)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - retried with backoff, holding the batch
                self._degraded = True
                if attempt == RETRY_ATTEMPTS - 1:
                    logger.error(
                        "%d event frames not journaled after %d attempts: %s",
                        len(frames), RETRY_ATTEMPTS, exc,
                    )
                    return False
                delay = RETRY_DELAYS_S[min(attempt, len(RETRY_DELAYS_S) - 1)]
                logger.warning(
                    "Task journal batch refused (%s); retrying in %.1fs holding %d frames",
                    exc, delay, len(frames),
                )
                await asyncio.sleep(delay)
                continue
            if self._degraded:
                self._degraded = False
                if self._on_db_recovered is not None:
                    try:
                        await self._on_db_recovered()
                    except Exception:  # noqa: BLE001
                        logger.warning("Post-outage reseed failed", exc_info=True)
            return True
        return False

    async def write_batch(self, frames: List[dict]) -> None:
        """One transaction for the batch; a frame that cannot be mapped is skipped on its own."""
        async with self._session_maker() as db:
            for frame in frames:
                try:
                    if frame.get("type") == "operation":
                        await self._write_operation(db, frame)
                    elif frame.get("type") == "lifecycle":
                        await self._write_lifecycle(db, frame)
                except asyncio.CancelledError:
                    raise
                except (ValueError, KeyError, TypeError) as exc:
                    logger.warning("Skipping an event frame the journal cannot map: %s", exc)
            await db.commit()

    async def _write_operation(self, db: Any, frame: dict) -> None:
        mapped = operation_frame(frame)
        if mapped is None:
            return
        await repository.upsert_operation(db, mapped)
        self.written += 1

    async def _write_lifecycle(self, db: Any, frame: dict) -> None:
        host = str(frame.get("host") or "")
        if not host or not frame.get("action"):
            return
        identities = await self._identities.for_host(host)
        row, key, (actor_kind, source, actor_email) = lifecycle_row(host, frame, identities)
        await repository.insert_log_rows(db, [row])
        self.written += 1
        if key is None:
            return
        requestor = row.get("details", {}).get("requestor") if row.get("details") else None
        if not requestor:
            return
        task_id = await repository.attach_requestor(
            db,
            key=key,
            requestor=requestor,
            actor_kind=actor_kind,
            source=source,
            actor_email=actor_email,
        )
        if task_id and row.get("event_id"):
            await repository.link_log_to_task(db, event_id=row["event_id"], task_id=task_id)
