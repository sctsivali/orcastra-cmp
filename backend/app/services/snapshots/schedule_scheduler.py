"""The loop that decides which schedules are due, and what to write down afterwards.

Rows are claimed with `FOR UPDATE SKIP LOCKED` instead of electing a leader, as in the webhook
dispatcher: the Redis lease fails open, so during a Redis outage all four workers would lead
and every schedule would fire four times. A claim per row degrades gracefully when a worker dies.

The reaper returns a row to `idle` with `due_slot` untouched, so the slot itself is retried. The
snapshot's name is derived from the slot, so the daemon refuses a second attempt instead of taking
a second copy. The dispatcher's reaper can retry a delivery because its receiver dedupes on an
event id; a duplicate snapshot has no such backstop.
"""

import asyncio
import logging
import os
import socket
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

from sqlalchemy import select, text

from app.models.snapshot_schedule_models import SnapshotSchedule
from app.services.lxd import health
from app.services.snapshots import recurrence
from app.services.snapshots.schedule_runner import RunOutcome, adopt_schedule_context, run_one

logger = logging.getLogger(__name__)

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"

# One fire can legitimately span a 30s read, several 30s deletes and a 20s settle, so the
# dispatcher's 120 would reap a healthy run mid-flight.
STUCK_AFTER_SECONDS = 180

# Consecutive failed runs before the schedule pauses. Failures are retried with the backoff below
# (2, 4, 8 and 16 minutes), so a persistent fault pauses a schedule after about half an hour.
PAUSE_AFTER_FAILURES = 5

# A failed run is retried within the current slot, so the backoff is in minutes.
_BACKOFF_BASE_SECONDS = 60
_BACKOFF_CAP_SECONDS = 1800

CLAIM_SQL = text(
    """
    UPDATE snapshot_schedules
       SET run_state = 'inflight', locked_by = :worker, locked_at = now(), updated_at = now()
     WHERE id IN (
         SELECT id FROM snapshot_schedules
          WHERE state = 'active' AND run_state = 'idle' AND next_run_at <= now()
          ORDER BY next_run_at
          FOR UPDATE SKIP LOCKED
          LIMIT :batch
     )
    RETURNING id
    """
)

# important: due_slot and next_run_at are not reset. The slot names the snapshot, so holding it
# keeps the retry idempotent.
REAP_SQL = text(
    """
    UPDATE snapshot_schedules
       SET run_state = 'idle', locked_by = NULL, locked_at = NULL, updated_at = now()
     WHERE run_state = 'inflight'
       AND locked_at < now() - make_interval(secs => :stuck)
    RETURNING id
    """
)

_task: Optional[asyncio.Task] = None


async def claim_batch(db: Any, *, batch: int = 8) -> List[int]:
    result = await db.execute(CLAIM_SQL, {"worker": WORKER_ID, "batch": batch})
    ids = [row[0] for row in result.all()]
    await db.commit()
    return ids


async def reap_stuck(db: Any) -> List[int]:
    """Rows a worker died holding. Without this they are claimed once and never again."""
    result = await db.execute(REAP_SQL, {"stuck": STUCK_AFTER_SECONDS})
    ids = [row[0] for row in result.all()]
    await db.commit()
    return ids


def _fields(schedule: SnapshotSchedule) -> tuple:
    return (schedule.frequency, schedule.hour_utc, schedule.minute_utc, schedule.weekday)


def choose_slot(schedule: SnapshotSchedule, now: datetime) -> datetime:
    """Which slot this run is for.

    A held slot is retried, which keeps a reaped row idempotent, until a newer slot has come round.
    Then the older one is abandoned: a catch-up snapshot would capture the current state under an
    old timestamp.
    """
    current = recurrence.latest_slot_at_or_before(*_fields(schedule), now)
    held = schedule.due_slot
    if held is None:
        return current
    if held.tzinfo is None:
        held = held.replace(tzinfo=timezone.utc)
    return current if current > held else held


def _backoff(failures: int) -> timedelta:
    seconds = min(_BACKOFF_BASE_SECONDS * (2 ** max(0, failures)), _BACKOFF_CAP_SECONDS)
    return timedelta(seconds=seconds)


def settle_row(schedule: SnapshotSchedule, outcome: RunOutcome, now: datetime) -> None:
    """Write down what happened. Pure state transition, so every branch is testable."""
    schedule.run_state = "idle"
    schedule.locked_by = None
    schedule.locked_at = None
    schedule.updated_at = now

    if outcome.result == "deferred":
        # A deferred run says nothing about the cluster's health. Nothing is counted and nothing
        # moves; the row is tried again on the next tick.
        return

    if outcome.result in ("success", "replayed"):
        slot = schedule.due_slot or now
        previous = schedule.last_run_slot
        if previous is not None:
            if previous.tzinfo is None:
                previous = previous.replace(tzinfo=timezone.utc)
            skipped = recurrence.slots_between(*_fields(schedule), previous, slot) - 1
            if skipped > 0:
                schedule.missed_runs = (schedule.missed_runs or 0) + skipped
        schedule.last_run_at = now
        schedule.last_run_slot = slot
        schedule.last_snapshot_name = outcome.snapshot
        schedule.last_error = None
        schedule.consecutive_failures = 0
        schedule.due_slot = None
        schedule.next_run_at = recurrence.next_slot_after(*_fields(schedule), slot)
        return

    # refused or failed
    message = outcome.refusal.message if outcome.refusal else (outcome.error or "The run failed.")
    schedule.last_error = message
    schedule.consecutive_failures = (schedule.consecutive_failures or 0) + 1

    if outcome.terminal or schedule.consecutive_failures >= PAUSE_AFTER_FAILURES:
        schedule.state = "paused"
        schedule.paused_reason = message
        schedule.due_slot = None
        return

    schedule.next_run_at = now + _backoff(schedule.consecutive_failures)


async def tick_once(
    session_maker: Any,
    lxd_service_factory: Any,
    *,
    now: Optional[datetime] = None,
    batch: int = 8,
) -> int:
    """One pass. Returns how many schedules were run."""
    moment = now or datetime.now(timezone.utc)
    ran = 0

    async with session_maker() as db:
        await reap_stuck(db)
        claimed = await claim_batch(db, batch=batch)

    if not claimed:
        return 0

    lxd_service = lxd_service_factory()
    for schedule_id in claimed:
        async with session_maker() as db:
            schedule = (
                await db.execute(
                    select(SnapshotSchedule).where(SnapshotSchedule.id == schedule_id)
                )
            ).scalar_one_or_none()
            if schedule is None:
                continue

            # important: checked before the work. A cluster whose breaker is open would otherwise
            # hold its claim for the full call timeout, and a serial batch would queue every other
            # schedule behind it.
            if health.is_tripped(schedule.cluster_id):
                schedule.run_state = "idle"
                schedule.locked_by = None
                schedule.locked_at = None
                schedule.updated_at = moment
                await db.commit()
                continue

            schedule.due_slot = choose_slot(schedule, moment)
            await db.commit()

            try:
                outcome = await run_one(
                    db, schedule, now=moment, lxd_service=lxd_service
                )
            except Exception as exc:  # noqa: BLE001 - one run must not end the loop
                logger.error(
                    "Schedule %s raised instead of returning an outcome", schedule_id, exc_info=True
                )
                outcome = RunOutcome(result="failed", error=str(exc))

            was_active = schedule.state == "active"
            missed_before = schedule.missed_runs or 0
            settle_row(schedule, outcome, moment)
            if (schedule.missed_runs or 0) > missed_before:
                adopt_schedule_context(schedule)
                _audit_missed(schedule, (schedule.missed_runs or 0) - missed_before)
            if was_active and schedule.state == "paused":
                adopt_schedule_context(schedule)
                _audit_paused(schedule)
            if outcome.result in ("success", "replayed"):
                ran += 1
            await db.commit()

    return ran


def _audit_missed(schedule: SnapshotSchedule, skipped: int) -> None:
    """Record coverage that was lost.

    Missed slots are not replayed, so this audit record is how an operator learns the gap existed.
    """
    from app.services.snapshots.schedule_runner import _audit

    _audit(schedule, "instance.snapshot.schedule.missed", "failure", skipped_slots=skipped)


def _audit_paused(schedule: SnapshotSchedule) -> None:
    from app.services.snapshots.schedule_runner import _audit

    _audit(
        schedule,
        "instance.snapshot.schedule.paused",
        "failure",
        reason=schedule.paused_reason,
        consecutive_failures=schedule.consecutive_failures,
    )


async def scheduler_loop(session_maker: Any, lxd_service_factory: Any, interval: float) -> None:
    while True:
        try:
            await tick_once(session_maker, lxd_service_factory)
        except asyncio.CancelledError:
            break
        except Exception:  # noqa: BLE001
            logger.error("Snapshot schedule tick failed", exc_info=True)
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break


def start_scheduler(session_maker: Any, lxd_service_factory: Any, *, interval: float = 30.0) -> None:
    """Every worker runs this; rows are claimed one by one, with no leader.

    A handle is kept, unlike the rebuild driver, so a shutdown can cancel the loop instead of
    leaving it holding a session while the process tears down.
    """
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(scheduler_loop(session_maker, lxd_service_factory, interval))


async def stop_scheduler() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    _task = None
