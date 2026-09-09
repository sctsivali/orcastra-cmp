"""The pre-restore safety snapshot is taken BEFORE the restore, and a failure aborts the restore.

Order is the whole point. Taken afterwards, the safety copy captures the already-rolled-back
state and protects nothing, while still costing pool space and looking like protection in the UI.

A restore discards every write since the snapshot, so this is the one control standing between a
mistaken click and unrecoverable data loss.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests._snapshot_fakes import HOST, INSTANCE, PROJECT, FakeClient, FakeInstanceNode, service

TARGET = "ops-target-20260908T000000Z"


@pytest.fixture(autouse=True)
def _no_webhook(monkeypatch):
    from app.routers import instance_snapshots

    monkeypatch.setattr(instance_snapshots, "schedule_orcahub_event", lambda *a, **k: None)


def _client(svc) -> TestClient:
    from app.core.org_scope import org_scope_dependency
    from app.core.rbac import get_current_user
    from app.routers import instance_snapshots
    from app.services.lxd_service import get_lxd_service

    app = FastAPI()
    app.include_router(instance_snapshots.router)
    app.dependency_overrides[get_lxd_service] = lambda: svc
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def _restore(node, **overrides):
    body = {"host": HOST, "project": PROJECT, "confirm_name": INSTANCE}
    body.update(overrides)
    return _client(service(FakeClient(node))).post(
        f"/instances/{INSTANCE}/snapshots/{TARGET}/restore", json=body
    )


def test_the_safety_snapshot_is_created_before_the_restore_is_issued():
    node = FakeInstanceNode(snapshots=[{"name": TARGET}])
    assert _restore(node).status_code == 200

    order = [call[0] for call in node.recorded if call[0] in ("create_snapshot", "put_instance")]
    assert order == ["create_snapshot", "put_instance"], (
        f"the safety snapshot must precede the restore, got {order}"
    )

    safety = [c for c in node.recorded if c[0] == "create_snapshot"][0][1]
    assert safety["name"].startswith("ops-pre-"), safety["name"]
    assert safety.get("expires_at"), "the safety snapshot must expire like any other"


def test_a_failed_safety_snapshot_aborts_the_restore():
    node = FakeInstanceNode(snapshots=[{"name": TARGET}])
    node.create_error = Exception("storage pool is full")

    response = _restore(node)
    assert response.status_code >= 400, response.text
    assert not [c for c in node.recorded if c[0] == "put_instance"], (
        "the restore must not run when its safety net could not be created"
    )


def test_the_typed_confirmation_must_match_the_instance():
    node = FakeInstanceNode(snapshots=[{"name": TARGET}])
    response = _restore(node, confirm_name="something-else")
    assert response.status_code == 422, response.text
    assert not node.recorded, "a refused restore must not have reached the daemon at all"


def test_an_operator_may_decline_the_safety_snapshot():
    node = FakeInstanceNode(snapshots=[{"name": TARGET}])
    assert _restore(node, safety_snapshot=False).status_code == 200
    assert not [c for c in node.recorded if c[0] == "create_snapshot"]
    assert [c for c in node.recorded if c[0] == "put_instance"]


# ── the three cases the old shape could not reach ───────────────────────────────────────────────

def test_a_safety_snapshot_the_daemon_accepts_then_fails_aborts_the_restore():
    """The defect this file existed to prevent and did not.

    `test_a_failed_safety_snapshot_aborts_the_restore` above sets `create_error`, which is a
    SYNCHRONOUS raise. The operation id was discarded, so an accepted-then-failed create was never
    observed at all and the restore proceeded on top of a safety copy that did not exist. Reachable
    now only because the operations fake answers per operation id.
    """
    node = FakeInstanceNode(snapshots=[{"name": TARGET}])
    node.operation_status = {"op-create-1": "Failure", "*": "Success"}
    node.operation_err = {"op-create-1": "storage pool is full"}

    response = _restore(node)
    assert response.status_code == 502, response.text
    assert "storage pool is full" in response.text
    assert not [c for c in node.recorded if c[0] == "put_instance"], (
        "the restore ran on top of a safety copy the daemon had failed"
    )
    # Left where it is: deleting it would be a second call on a failure path that can also fail,
    # and it carries its own expires_at.
    assert [c for c in node.recorded if c[0] == "create_snapshot"]


def test_a_safety_snapshot_still_being_written_defers_the_restore():
    """Nothing failed. Restoring over a copy mid-write is what LXD answers 409 for, so this does."""
    node = FakeInstanceNode(snapshots=[{"name": TARGET}])
    node.operation_status = {"op-create-1": "Running", "*": "Success"}

    response = _restore(node)
    assert response.status_code == 409, response.text
    assert "still being written" in response.text
    assert not [c for c in node.recorded if c[0] == "put_instance"]


def test_the_safety_snapshot_is_counted_against_the_cap():
    """`assert_capacity` was called only on the create path, so restoring twice silently filled two
    of five slots and the operator's next manual snapshot was refused about their own."""
    from app.services.lxd.snapshots import MAX_OPS_SNAPSHOTS_PER_INSTANCE

    # TARGET is itself an ops- name and therefore counts, so hold one fewer to reach exactly the cap.
    held = [
        {"name": f"ops-held-{i}-20260909T0300{i}0Z"}
        for i in range(MAX_OPS_SNAPSHOTS_PER_INSTANCE - 1)
    ]
    node = FakeInstanceNode(snapshots=held + [{"name": TARGET}])

    response = _restore(node)
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "safety_snapshot_capacity_exhausted"
    # The affordance is named rather than left to be inferred.
    assert detail["proceed_field"] == "safety_snapshot"
    assert len(detail["operator_snapshots"]) == MAX_OPS_SNAPSHOTS_PER_INSTANCE
    assert not [c for c in node.recorded if c[0] == "create_snapshot"]
    assert not [c for c in node.recorded if c[0] == "put_instance"]


def test_declining_the_safety_snapshot_gets_past_a_full_instance():
    """The affordance the refusal names has to actually work, or naming it is worse than silence."""
    from app.services.lxd.snapshots import MAX_OPS_SNAPSHOTS_PER_INSTANCE

    held = [
        {"name": f"ops-held-{i}-20260909T0300{i}0Z"}
        for i in range(MAX_OPS_SNAPSHOTS_PER_INSTANCE - 1)
    ]
    node = FakeInstanceNode(snapshots=held + [{"name": TARGET}])

    response = _restore(node, safety_snapshot=False)
    assert response.status_code == 200, response.text
    assert [c for c in node.recorded if c[0] == "put_instance"]


def test_the_safety_operation_is_waited_on_before_the_restore_is_issued():
    """Order, not just outcome: create, then wait, then restore."""
    node = FakeInstanceNode(snapshots=[{"name": TARGET}])
    assert _restore(node).status_code == 200

    order = [c[0] for c in node.recorded if c[0] in ("create_snapshot", "wait_operation", "put_instance")]
    assert order[:3] == ["create_snapshot", "wait_operation", "put_instance"], order


def test_the_restore_leaves_no_lock_behind():
    """`rebuild_lock` registers a waiter; only `release_lock` drops the entry.

    The registry is process-wide and module-level, so a gate that takes the lock without the
    matching release grows one dict entry per (host, project, instance) for the life of the worker.
    That is the accumulation `core/single_flight.py:45-47` states does not happen, and it was found
    by an unrelated test in another file failing on the residue rather than here, which is exactly
    the ordering dependence that makes such a leak hard to attribute.

    important: asserted on the KEY, and on `_waiters` as well as `_locks`. Comparing the whole dict
    against a snapshot taken at the top of this test passes either way, because every restore in
    this file uses one host, project and instance, so the leaked entry is already there before the
    first assertion runs. That version of this test was green against the unfixed gate.

    All three exits, because the acquire is what leaks and every refusal raises past it.
    """
    from app.core import single_flight as sf
    from app.services.lxd.snapshots import MAX_OPS_SNAPSHOTS_PER_INSTANCE

    key = f"snapshot-create:{HOST}/{PROJECT}/{INSTANCE}"

    def assert_clean(what: str) -> None:
        assert key not in sf._locks, f"{what} kept its lock"
        assert key not in sf._waiters, f"{what} kept its waiter count"

    sf._locks.pop(key, None)
    sf._waiters.pop(key, None)

    assert _restore(FakeInstanceNode(snapshots=[{"name": TARGET}])).status_code == 200
    assert_clean("the success path")

    held = [
        {"name": f"ops-held-{i}-20260909T0300{i}0Z"}
        for i in range(MAX_OPS_SNAPSHOTS_PER_INSTANCE - 1)
    ]
    assert _restore(FakeInstanceNode(snapshots=held + [{"name": TARGET}])).status_code == 409
    assert_clean("the capacity refusal")

    failed = FakeInstanceNode(snapshots=[{"name": TARGET}])
    failed.operation_status = {"op-create-1": "Failure", "*": "Success"}
    assert _restore(failed).status_code == 502
    assert_clean("the failed-operation refusal")
