"""At most one snapshot per schedule per slot, with the cluster itself as the dedupe store.

Exactly-once is not available across a network. Instead the snapshot's name is derived from the
due slot rather than from `now`, and LXD refuses a name it already holds. A worker that died
after the daemon created the snapshot but before its row was updated retries the same slot,
composes the same name, and is refused rather than taking a second copy.

The refusal arrives asynchronously as the operation's error, never as an HTTP status:

    Failed creating instance snapshot record "ops-...":
    Snapshot "ct-.../ops-..." already exists

which is why `_classify_create_failure` reads the settled operation and not the response code.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from app.services.snapshots.naming import build_scheduled_snapshot_name
from app.services.snapshots.schedule_runner import run_one
from app.services.snapshots.schedule_scheduler import REAP_SQL, choose_slot, settle_row
from tests._schedule_fakes import FakeDB, FakeSchedule, ops_rows
from tests._snapshot_fakes import FakeClient, FakeInstanceNode, service

UTC = timezone.utc
SLOT = datetime(2026, 9, 9, 3, 0, tzinfo=UTC)

DUPLICATE_ERR = (
    'Failed creating instance snapshot record "ops-sch12-nightly-20260909T030000Z": '
    'Snapshot "ct-qa-fixture-0001/ops-sch12-nightly-20260909T030000Z" already exists'
)


def test_the_name_comes_from_the_slot_and_not_from_the_clock():
    """Two runs of the same slot, minutes apart, compose one name."""
    early = build_scheduled_snapshot_name(12, "nightly", SLOT)
    late = build_scheduled_snapshot_name(12, "nightly", SLOT)
    assert early == late
    assert build_scheduled_snapshot_name(12, "nightly", SLOT + timedelta(days=1)) != early


def test_a_replayed_slot_is_recorded_as_done_rather_than_failed():
    node = FakeInstanceNode(snapshots=ops_rows(12, 1))
    node.operation_status = "Failure"
    node.operation_err = DUPLICATE_ERR
    outcome = asyncio.run(
        run_one(
            FakeDB(),
            FakeSchedule(keep_last=3, due_slot=SLOT),
            now=SLOT,
            lxd_service=service(FakeClient(node)),
        )
    )
    assert outcome.result == "replayed"
    assert outcome.snapshot == build_scheduled_snapshot_name(12, "nightly", SLOT)


def test_a_replayed_slot_advances_exactly_once():
    schedule = FakeSchedule(keep_last=3, due_slot=SLOT)
    node = FakeInstanceNode(snapshots=ops_rows(12, 1))
    node.operation_status = "Failure"
    node.operation_err = DUPLICATE_ERR
    outcome = asyncio.run(
        run_one(FakeDB(), schedule, now=SLOT, lxd_service=service(FakeClient(node)))
    )
    settle_row(schedule, outcome, SLOT)
    assert schedule.last_run_slot == SLOT
    assert schedule.due_slot is None
    assert schedule.next_run_at == datetime(2026, 9, 10, 3, 0, tzinfo=UTC)
    assert schedule.consecutive_failures == 0


def test_a_genuine_failure_is_not_mistaken_for_a_replay():
    """The control. Matching too loosely would turn every failure into a silent success."""
    node = FakeInstanceNode(snapshots=ops_rows(12, 1))
    node.operation_status = "Failure"
    node.operation_err = "no space left on device"
    outcome = asyncio.run(
        run_one(
            FakeDB(),
            FakeSchedule(keep_last=3, due_slot=SLOT),
            now=SLOT,
            lxd_service=service(FakeClient(node)),
        )
    )
    assert outcome.result == "failed"
    assert "no space" in outcome.error


def test_the_reaper_returns_the_row_without_touching_its_slot():
    """Unlike the webhook dispatcher's reaper, this one retries the slot.

    The webhook reaper returns a row to `pending` so the delivery is retried. This one returns it
    to `idle` with `due_slot` untouched so the slot is retried, which the slot-derived name makes
    safe. A duplicate delivery is tolerable because the receiver dedupes on an event id; a
    duplicate snapshot has no such backstop.
    """
    sql = str(REAP_SQL)
    assert "run_state = 'idle'" in sql
    assert "due_slot" not in sql, "the reaper moved the slot, so a retry would rename itself"
    assert "next_run_at" not in sql


def test_a_held_slot_is_retried_until_a_newer_one_comes_round():
    schedule = FakeSchedule(due_slot=SLOT)
    # Same day, an hour later: still the same slot, so the retry is idempotent.
    assert choose_slot(schedule, SLOT + timedelta(hours=1)) == SLOT
    # A day later a newer slot exists, so the stale one is abandoned rather than chased.
    assert choose_slot(schedule, SLOT + timedelta(days=1, hours=1)) == SLOT + timedelta(days=1)


def test_an_accepted_but_unconfirmable_create_is_not_reported_as_done():
    """A 202 naming no operation is never reported as a snapshot taken.

    `_operation_id` raises rather than treating it as the synchronous case: the daemon accepted
    work that may still be running, and reading that as done would announce a restore point that
    may not exist. The slot stays held so the retry gets another chance, and the slot-derived
    name makes that retry safe either way.
    """
    schedule = FakeSchedule(keep_last=3, due_slot=SLOT)
    node = FakeInstanceNode(snapshots=ops_rows(12, 1))
    node.omit_operation = True
    outcome = asyncio.run(
        run_one(FakeDB(), schedule, now=SLOT, lxd_service=service(FakeClient(node)))
    )
    assert outcome.result == "failed"
    assert "cannot be confirmed" in (outcome.error or "")

    settle_row(schedule, outcome, SLOT)
    assert schedule.due_slot == SLOT, "the slot was advanced past a snapshot nobody confirmed"
    assert schedule.consecutive_failures == 1
