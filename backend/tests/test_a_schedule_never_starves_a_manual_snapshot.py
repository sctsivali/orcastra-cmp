"""A schedule must never be the reason an operator cannot take a snapshot by hand.

`MAX_OPS_SNAPSHOTS_PER_INSTANCE` counts every `ops-` name on the cluster, including the ones an
operator took and the `ops-pre-` safety copies a restore leaves behind. A schedule allowed to keep
that many would fill the budget and turn the pre-maintenance snapshot into a 409. The schedule's
ceiling is therefore strictly below the instance's, and the gap is the operator's.
"""

import asyncio
from datetime import datetime, timezone

import pytest

from app.services.lxd.snapshots import MAX_OPS_SNAPSHOTS_PER_INSTANCE
from app.services.snapshots.limits import MAX_SCHEDULE_KEEP_LAST, MAX_SCHEDULE_LABEL
from app.services.snapshots.naming import build_scheduled_snapshot_name, scheduled_snapshot_prefix
from app.services.snapshots.schedule_runner import _select_own, run_one
from tests._schedule_fakes import FakeDB, FakeSchedule, ops_rows, snapshot_row
from tests._snapshot_fakes import FakeClient, FakeInstanceNode, service

UTC = timezone.utc
SLOT = datetime(2026, 9, 9, 3, 0, tzinfo=UTC)


def test_the_schedule_ceiling_is_strictly_below_the_instance_ceiling():
    """A schedule at the instance's own ceiling leaves no room for the snapshot an operator takes
    before a change."""
    assert MAX_SCHEDULE_KEEP_LAST < MAX_OPS_SNAPSHOTS_PER_INSTANCE
    headroom = MAX_OPS_SNAPSHOTS_PER_INSTANCE - MAX_SCHEDULE_KEEP_LAST
    assert headroom >= 2, "an operator needs room for a manual snapshot and a pre-restore copy"


def test_the_label_bound_is_what_the_name_can_actually_hold():
    """The label bound is 63 minus 'ops-', a 16-character stamp and the joining dash.

    Bounded on the label rather than on the result, so an over-long label is refused as an input
    instead of discovered later as two schedules rendering to one name.
    """
    longest = build_scheduled_snapshot_name(999, "x" * MAX_SCHEDULE_LABEL, SLOT)
    assert len(longest) <= 63, longest


def test_retention_finds_this_schedules_snapshots_and_nobody_elses():
    prefix = scheduled_snapshot_prefix(1)
    rows = [
        snapshot_row("ops-sch1-nightly-20260901T030000Z", "2026-09-01T03:00:00Z"),
        # A sibling schedule whose id merely starts with the same digit.
        snapshot_row("ops-sch12-nightly-20260901T030000Z", "2026-09-01T03:00:00Z"),
        # An operator's own, taken by hand this morning.
        snapshot_row("ops-maintenance-20260909T090000Z", "2026-09-09T09:00:00Z"),
        # A restore's safety copy.
        snapshot_row("ops-pre-ct-x-20260909T080000Z", "2026-09-09T08:00:00Z"),
        # The metered product's.
        snapshot_row("snap-20260909-0200", "2026-09-09T02:00:00Z"),
    ]
    mine = [row["name"] for row in _select_own(rows, prefix)]
    assert mine == ["ops-sch1-nightly-20260901T030000Z"]


def test_retention_returns_them_oldest_first():
    rows = list(reversed(ops_rows(12, 3)))
    ordered = [row["name"] for row in _select_own(rows, scheduled_snapshot_prefix(12))]
    assert ordered == [row["name"] for row in ops_rows(12, 3)]


@pytest.mark.parametrize("keep_last", [1, 2, 3])
def test_a_full_schedule_leaves_room_for_a_manual_snapshot(keep_last):
    """Steady state: run the schedule until it is at its own ceiling, then count what is free."""
    node = FakeInstanceNode(snapshots=ops_rows(12, keep_last))
    schedule = FakeSchedule(keep_last=keep_last, due_slot=SLOT)
    svc = service(FakeClient(node))

    outcome = asyncio.run(run_one(FakeDB(), schedule, now=SLOT, lxd_service=svc))

    # Without this, a run that refused and changed nothing would satisfy the count below.
    assert outcome.result == "success", f"{outcome.result}: {outcome.error or outcome.refusal}"
    assert outcome.snapshot == build_scheduled_snapshot_name(schedule.id, schedule.label, SLOT)

    held = [s["name"] for s in node.snapshots]
    assert len(held) == keep_last, held
    assert outcome.snapshot in held
    assert MAX_OPS_SNAPSHOTS_PER_INSTANCE - len(held) >= 2


def test_pruning_never_touches_a_snapshot_the_schedule_did_not_make():
    manual = snapshot_row("ops-maintenance-20260909T090000Z", "2026-09-09T09:00:00Z")
    sibling = snapshot_row("ops-sch99-other-20260901T030000Z", "2026-09-01T03:00:00Z")
    node = FakeInstanceNode(snapshots=ops_rows(12, 3) + [manual, sibling])
    schedule = FakeSchedule(keep_last=1, due_slot=SLOT)
    svc = service(FakeClient(node))

    outcome = asyncio.run(run_one(FakeDB(), schedule, now=SLOT, lxd_service=svc))
    assert outcome.result == "success", f"{outcome.result}: {outcome.error or outcome.refusal}"

    surviving = {s["name"] for s in node.snapshots}
    assert manual["name"] in surviving, "an operator's own snapshot was pruned by a schedule"
    assert sibling["name"] in surviving, "another schedule's snapshot was pruned"
    deleted = [entry[1] for entry in node.recorded if entry[0] == "delete_snapshot"]
    assert all(name.startswith(scheduled_snapshot_prefix(12)) for name in deleted), deleted
