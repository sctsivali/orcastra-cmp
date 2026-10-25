"""A restore the instance's storage cannot perform is refused before anything is written.

ZFS rolls back only to the newest snapshot. The console's safety copy is by construction newer than
the target, so on ZFS every restore with a safety copy was refused by the daemon after the copy had
been written (seen on nuc2, 2026-10-25). The plan is read first, and a restore that cannot run is a
409 with the reason, with no safety copy left behind.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.snapshots.restore_plan import plan_restore
from tests._snapshot_fakes import HOST, INSTANCE, PROJECT, FakeClient, FakeInstanceNode, service

OLD = "ops-old-20261020T000000Z"
NEW = "ops-new-20261024T000000Z"
ROWS = [
    {"name": OLD, "created_at": "2026-10-20T00:00:00.123456789Z"},
    {"name": NEW, "created_at": "2026-10-24T00:00:00Z"},
]


@pytest.fixture(autouse=True)
def _no_webhook(monkeypatch):
    from app.routers import instance_snapshots

    monkeypatch.setattr(instance_snapshots, "schedule_orcahub_event", lambda *a, **k: None)


@pytest.fixture
def audited(monkeypatch):
    from app.routers import instance_snapshots

    rows = []
    monkeypatch.setattr(
        instance_snapshots.activity_logger, "log_activity", lambda **kw: rows.append(kw)
    )
    return rows


def _app(node) -> TestClient:
    from app.core.org_scope import org_scope_dependency
    from app.core.rbac import get_current_user
    from app.routers import instance_snapshot_follow, instance_snapshots
    from app.services.lxd_service import get_lxd_service

    app = FastAPI()
    app.include_router(instance_snapshots.router)
    app.include_router(instance_snapshot_follow.router)
    svc = service(FakeClient(node))
    app.dependency_overrides[get_lxd_service] = lambda: svc
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def _node(driver: str) -> FakeInstanceNode:
    node = FakeInstanceNode(snapshots=[dict(row) for row in ROWS])
    node.root_pool = "default"
    node.pool_driver = driver
    return node


def _restore(node, name, **overrides):
    body = {"host": HOST, "project": PROJECT, "confirm_name": INSTANCE, **overrides}
    return _app(node).post(f"/instances/{INSTANCE}/snapshots/{name}/restore", json=body)


def _writes(node):
    return [c[0] for c in node.recorded if c[0] in ("create_snapshot", "put_instance")]


# ── the plan ───────────────────────────────────────────────────────────────────────────────


def test_a_driver_that_restores_any_snapshot_plans_no_limit():
    plan = plan_restore("dir", ROWS, OLD)
    assert plan.restorable and plan.safety_snapshot_supported and plan.reason is None


def test_zfs_restores_its_newest_snapshot_without_a_safety_copy():
    plan = plan_restore("zfs", ROWS, NEW)
    assert plan.restorable is True
    assert plan.safety_snapshot_supported is False
    assert plan.newer_snapshots == []


def test_zfs_refuses_an_older_snapshot_and_names_what_is_newer():
    plan = plan_restore("zfs", ROWS, OLD)
    assert plan.restorable is False
    assert plan.newer_snapshots == [NEW]
    assert NEW in plan.reason and "newest" in plan.reason


def test_nanosecond_times_are_compared_as_times_not_as_text():
    """"...:29Z" sorts after "...:29.5Z" as text although it is half a second earlier."""
    rows = [
        {"name": "a", "created_at": "2026-10-25T10:04:29.500000001Z"},
        {"name": "b", "created_at": "2026-10-25T10:04:29Z"},
    ]
    assert plan_restore("zfs", rows, "b").newer_snapshots == ["a"]
    assert plan_restore("zfs", rows, "a").newer_snapshots == []


def test_without_times_the_listing_order_decides():
    rows = [{"name": "a"}, {"name": "b"}]
    assert plan_restore("zfs", rows, "a").newer_snapshots == ["b"]


# ── the restore route ──────────────────────────────────────────────────────────────────────


def test_zfs_refuses_an_older_snapshot_before_anything_is_written(audited):
    node = _node("zfs")
    response = _restore(node, OLD)
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "restore_needs_newest_snapshot"
    assert NEW in detail["message"]
    assert _writes(node) == [], "no safety copy and no restore may be sent"
    denied = [row for row in audited if row.get("result") == "denied"]
    assert [row["error_message"] for row in denied] == ["restore_needs_newest_snapshot"], audited


def test_zfs_refuses_a_safety_copy_before_anything_is_written():
    node = _node("zfs")
    response = _restore(node, NEW)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"] == "safety_snapshot_unsupported"
    assert _writes(node) == []


def test_zfs_restores_the_newest_snapshot_when_no_safety_copy_is_asked_for():
    node = _node("zfs")
    response = _restore(node, NEW, safety_snapshot=False)
    assert response.status_code == 200, response.text
    assert _writes(node) == ["put_instance"]


def test_other_drivers_keep_the_safety_copy_then_restore():
    node = _node("dir")
    response = _restore(node, OLD)
    assert response.status_code == 200, response.text
    assert _writes(node) == ["create_snapshot", "put_instance"]


def test_an_unreadable_driver_proceeds_as_before(monkeypatch):
    """Unknown is not refused: the daemon's own refusal is now reported, so nothing is hidden."""
    from app.services.snapshots import restore_plan

    def _boom(*a, **k):
        raise RuntimeError("storage pool read timed out")

    monkeypatch.setattr(restore_plan, "read_root_pool_driver", _boom)
    node = _node("zfs")
    response = _restore(node, OLD, safety_snapshot=False)
    assert response.status_code == 200, response.text
    assert _writes(node) == ["put_instance"]


# ── the plan endpoint ──────────────────────────────────────────────────────────────────────


def test_the_plan_is_readable_before_the_dialog_submits():
    node = _node("zfs")
    response = _app(node).get(
        f"/instances/{INSTANCE}/snapshots/{OLD}/restore-plan",
        params={"host": HOST, "project": PROJECT},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["storage_driver"] == "zfs"
    assert body["restorable"] is False and body["safety_snapshot_supported"] is False
    assert body["newer_snapshots"] == [NEW]
    assert _writes(node) == []


def test_the_plan_of_a_missing_snapshot_is_a_404():
    response = _app(_node("zfs")).get(
        f"/instances/{INSTANCE}/snapshots/ops-gone-20261001T000000Z/restore-plan",
        params={"host": HOST, "project": PROJECT},
    )
    assert response.status_code == 404, response.text


def test_a_caller_without_snapshot_access_cannot_read_the_plan(monkeypatch):
    from fastapi import HTTPException

    from app.routers import instance_snapshots

    def _refuse(host, project, instance, permission):
        raise HTTPException(status_code=403, detail="no snapshot permission")

    monkeypatch.setattr(instance_snapshots, "verify_tenant_permission", _refuse)
    node = _node("zfs")
    response = _app(node).get(
        f"/instances/{INSTANCE}/snapshots/{OLD}/restore-plan",
        params={"host": HOST, "project": PROJECT},
    )
    assert response.status_code == 403, response.text
    assert node.recorded == [], "the cluster was asked before the gate"
