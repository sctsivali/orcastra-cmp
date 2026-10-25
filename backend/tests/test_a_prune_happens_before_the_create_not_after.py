"""Retention runs before the snapshot is taken, and the order is the whole point.

Prune-after-create means peak occupancy is `keep_last + 1`, and that peak lands at exactly the
instant a concurrent manual snapshot would be refused by `assert_capacity`. Pruning first means the
schedule never occupies more than `keep_last`, so the two spare slots stay the operator's at every
moment rather than most of them.

It also decides what a genuinely full instance means. With the prune already done, a capacity
refusal can only be the operator's OWN snapshots filling the instance, so the refusal names theirs
instead of the schedule quietly deleting something it does not own to make room.
"""

import asyncio
from datetime import datetime, timezone

from app.services.snapshots.schedule_runner import run_one
from tests._schedule_fakes import FakeDB, FakeSchedule, ops_rows, snapshot_row
from tests._snapshot_fakes import FakeClient, FakeInstanceNode, service

UTC = timezone.utc
SLOT = datetime(2026, 9, 9, 3, 0, tzinfo=UTC)


def _labels(node) -> list:
    return [entry[0] for entry in node.recorded]


def test_the_delete_is_recorded_before_the_create():
    node = FakeInstanceNode(snapshots=ops_rows(12, 3))
    svc = service(FakeClient(node))
    outcome = asyncio.run(
        run_one(FakeDB(), FakeSchedule(keep_last=3, due_slot=SLOT), now=SLOT, lxd_service=svc)
    )
    assert outcome.result == "success", outcome.error

    order = _labels(node)
    assert "delete_snapshot" in order, "nothing was pruned, so the order proves nothing"
    assert order.index("delete_snapshot") < order.index("create_snapshot"), order


def test_peak_occupancy_never_exceeds_keep_last():
    """Fire the same schedule repeatedly and watch the high-water mark, not just the end state."""
    node = FakeInstanceNode(snapshots=ops_rows(12, 3))
    svc = service(FakeClient(node))
    peak = len(node.snapshots)
    for day in range(10, 16):
        slot = datetime(2026, 9, day, 3, 0, tzinfo=UTC)
        outcome = asyncio.run(
            run_one(FakeDB(), FakeSchedule(keep_last=3, due_slot=slot), now=slot, lxd_service=svc)
        )
        assert outcome.result == "success", outcome.error
        peak = max(peak, len(node.snapshots))
    assert peak == 3, f"the schedule held {peak} snapshots at once, above its own ceiling"


def test_a_full_instance_refuses_and_names_the_operators_own_snapshots():
    """Five snapshots an operator took by hand. The schedule has nothing of its own to prune, so
    the refusal has to be about theirs rather than the schedule deleting one to make room."""
    manual = [
        snapshot_row(f"ops-maintenance-{i}-20260909T090000Z", f"2026-09-0{i}T09:00:00Z")
        for i in range(1, 6)
    ]
    node = FakeInstanceNode(snapshots=list(manual))
    svc = service(FakeClient(node))
    outcome = asyncio.run(
        run_one(FakeDB(), FakeSchedule(keep_last=3, due_slot=SLOT), now=SLOT, lxd_service=svc)
    )

    assert outcome.result == "failed"
    assert "delete_snapshot" not in _labels(node), "the schedule pruned a snapshot it does not own"
    assert len(node.snapshots) == 5
    assert "create_snapshot" not in _labels(node)


def test_a_prune_that_fails_does_not_cancel_the_snapshot():
    """A cleanup failing is not a reason to lose a restore point.

    Every snapshot carries its own `expires_at`, so LXD removes the overshoot without help. Skipping
    the create to protect the prune would trade the thing that matters for the thing that does not.
    """
    node = FakeInstanceNode(snapshots=ops_rows(12, 3))
    svc = service(FakeClient(node))

    import app.services.lxd.snapshots as snapshots_service

    real_delete = snapshots_service.delete_instance_snapshot

    def _boom(client, instance_name, project, snapshot_name):
        raise RuntimeError("the daemon refused the delete")

    snapshots_service.delete_instance_snapshot = _boom
    try:
        outcome = asyncio.run(
            run_one(FakeDB(), FakeSchedule(keep_last=3, due_slot=SLOT), now=SLOT, lxd_service=svc)
        )
    finally:
        snapshots_service.delete_instance_snapshot = real_delete

    assert outcome.result == "success", outcome.error
    assert outcome.pruned == []
    assert "create_snapshot" in _labels(node)


def test_a_prune_the_daemon_accepts_then_fails_is_not_counted_as_pruned():
    """The delete is accepted (202) and its operation then fails.

    LXD answers the wait of a failed operation with an error-typed body. The wait used to raise on
    that, which is the only way this loop learned the prune had failed; it now reads the failure
    back, so the loop has to look at it or the failed prune is audited as a success.
    """
    node = FakeInstanceNode(snapshots=ops_rows(12, 3))
    node.operation_status = {"op-delete": "Failure", "*": "Success"}
    node.operation_err = {"op-delete": "snapshot is in use"}
    svc = service(FakeClient(node))

    outcome = asyncio.run(
        run_one(FakeDB(), FakeSchedule(keep_last=3, due_slot=SLOT), now=SLOT, lxd_service=svc)
    )

    assert outcome.result == "success", outcome.error
    assert outcome.pruned == [], "a prune the daemon failed was counted as done"
    assert "create_snapshot" in _labels(node)
