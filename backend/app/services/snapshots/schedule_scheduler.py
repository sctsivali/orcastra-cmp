"""The loop that decides which schedules are due, and what to write down afterwards.

Rows are claimed with `FOR UPDATE SKIP LOCKED`, not by electing a leader. That is this codebase's
own settled answer for exactly this shape of work, and the reason is written down beside the
webhook dispatcher: the one lease here deliberately fails open, so during a Redis outage all four
workers would believe they lead and every schedule would fire four times. Many independent rows and
a claim per row degrades gracefully when a worker dies; a lease does the opposite.

The reaper differs from the dispatcher's in one deliberate way. That one returns a row to `pending`
so the DELIVERY is retried. This returns a row to `idle` with `due_slot` untouched, so the SLOT is
retried, and because the snapshot's name is derived from the slot the daemon refuses the second
attempt instead of taking a second copy. The dispatcher does not need that property because its
receiver dedupes on an event id; a duplicate snapshot has no such backstop.
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

# Five, not the dispatcher's twenty. Twenty is right there because it retries a ten-second POST and
# twenty failures is under an hour. A schedule's tick is hours, so twenty would be weeks of
# silence: about thirty hours on every_6_hours and five days on daily, which is still inside the
# window where an operator recognises the instance.
PAUSE_AFTER_FAILURES = 5

# Minutes, not hours. The period is already hours, so this only decides how soon a transient
# cluster fault is retried within the current slot.
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

# important: due_slot and next_run_at are deliberately NOT reset. The slot is what the snapshot is
# named after, so holding it is what makes the retry idempotent.
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

    A held slot is retried, which is what makes a reaped row idempotent, but only until a newer
    slot has come round. Past that the older one is abandoned rather than chased: every catch-up
    snapshot would capture the same state, the current one, so a day of them is a day of restore
    points wearing misleading timestamps rather than a day anyone can go back to.
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
        # Our own load, not the cluster's health. Nothing is counted and nothing moves; the row is
        # simply tried again on the next tick.
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

            # important: consulted BEFORE the work, not inside it. A cluster whose breaker is open
            # would otherwise be claimed and held for the full call timeout, and a serial batch
            # would queue every other schedule behind it.
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
            except Exception as exc:  # noqa: BLE001 - a loop that can die is a loop that will
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
    """Coverage that was lost, said out loud.

    Nothing is replayed, on purpose: every catch-up snapshot would capture the same state, the
    current one, so a day of them is a day of restore points wearing misleading timestamps. This
    is what tells an operator the gap existed instead of leaving them to infer it from a list.
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
    """Every worker runs this. Rows are claimed, not led.

    A handle is kept, unlike the rebuild driver beside it, so a shutdown can cancel the loop
    rather than leave it holding a session while the process tears down.
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
