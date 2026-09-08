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
