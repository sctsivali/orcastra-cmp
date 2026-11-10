"""A restore LXD refuses because the project sets limits is answered with a reason and a way out.

LXD 5.21.5 and later re-check project policy on a snapshot restore and refuse it when a
`volatile.*` key would change: `Changing "volatile.uuid.generation" on container "x" in project "p"
is forbidden`. Most restores change one, so in a project with `limits.*` only an instance's first
restore passes. Clearing the project's limits lets every restore through, and setting them again
brings the refusal back.

The daemon text alone reads like a broken instance, so the answer is a 409 that names the project's
limits as the lever.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests._snapshot_fakes import HOST, INSTANCE, PROJECT, FakeClient, FakeInstanceNode, service

TARGET = "ops-target-20261105T000000Z"
LXD_REFUSAL = (
    f'Changing "volatile.uuid.generation" on container "{INSTANCE}" in project "{PROJECT}" is forbidden'
)


@pytest.fixture(autouse=True)
def _no_webhook(monkeypatch):
    from app.routers import instance_snapshots

    monkeypatch.setattr(instance_snapshots, "schedule_orcahub_event", lambda *a, **k: None)


def _restore(node, **overrides):
    from app.core.org_scope import org_scope_dependency
    from app.core.rbac import get_current_user
    from app.routers import instance_snapshots
    from app.services.lxd_service import get_lxd_service

    app = FastAPI()
    app.include_router(instance_snapshots.router)
    app.dependency_overrides[get_lxd_service] = lambda: service(FakeClient(node))
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    body = {"host": HOST, "project": PROJECT, "confirm_name": INSTANCE, "safety_snapshot": False}
    body.update(overrides)
    return TestClient(app, raise_server_exceptions=False).post(
        f"/instances/{INSTANCE}/snapshots/{TARGET}/restore", json=body
    )


def _refused_by_lxd(err: str) -> FakeInstanceNode:
    node = FakeInstanceNode(snapshots=[{"name": TARGET}])
    node.operation_status = {"op-restore": "Failure", "*": "Success"}
    node.operation_err = {"op-restore": err}
    return node


@pytest.mark.parametrize("key", ["volatile.uuid.generation", "volatile.eth0.host_name"])
def test_a_volatile_key_refusal_names_the_project_limits_and_the_way_out(key):
    response = _restore(_refused_by_lxd(LXD_REFUSAL.replace("volatile.uuid.generation", key)))

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "restore_refused_by_project_limits"
    assert PROJECT in detail["message"] and "limits" in detail["message"]
    assert "set them again" in detail["message"], "the message must say how to restore anyway"
    assert key in detail["lxd_error"], "the daemon's own words stay available"


def test_another_daemon_failure_still_answers_bad_gateway():
    response = _restore(_refused_by_lxd("disk quota exceeded"))

    assert response.status_code == 502, response.text
    assert "disk quota exceeded" in response.text


def test_the_safety_snapshot_is_named_when_the_refused_restore_kept_one():
    node = _refused_by_lxd(LXD_REFUSAL)
    response = _restore(node, safety_snapshot=True)

    assert response.status_code == 409, response.text
    assert "ops-pre-" in response.json()["detail"]["message"]
