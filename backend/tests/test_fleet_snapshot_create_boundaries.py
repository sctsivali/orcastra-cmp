"""Fleet snapshot create must preserve scope and publish only confirmed effects."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Response
from fastapi.testclient import TestClient

from app.core import org_scope
from app.core.config import get_settings
from app.models.schemas import SnapshotCreateRequest
from app.routers import instance_snapshots
from tests._snapshot_fakes import HOST, INSTANCE, PROJECT, FakeClient, FakeInstanceNode, service


FOREIGN_PROJECT = "another-partners-project"


@pytest.fixture
def create_side_effects(monkeypatch):
    audits = []
    webhooks = []
    invalidations = []

    class RecordingCache:
        def invalidate_resource_cache(self, resource):
            invalidations.append(resource)

    monkeypatch.setattr(
        instance_snapshots.activity_logger,
        "log_activity",
        lambda **kwargs: audits.append(kwargs),
    )
    monkeypatch.setattr(
        instance_snapshots,
        "schedule_orcahub_event",
        lambda **kwargs: webhooks.append(kwargs),
    )
    monkeypatch.setattr(instance_snapshots, "get_cache_manager", lambda: RecordingCache())
    return SimpleNamespace(
        audits=audits,
        webhooks=webhooks,
        invalidations=invalidations,
    )


def _client(node: FakeInstanceNode) -> TestClient:
    from app.core.org_scope import org_scope_dependency
    from app.core.rbac import get_current_user
    from app.services.lxd_service import get_lxd_service

    app = FastAPI()
    app.include_router(instance_snapshots.router)
    app.dependency_overrides[get_lxd_service] = lambda: service(FakeClient(node))
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def _create_direct(project: str):
    return asyncio.run(
        instance_snapshots.create_snapshot(
            instance_name=INSTANCE,
            body=SnapshotCreateRequest(host=HOST, project=project, label="scope check"),
            response=Response(),
            lxd_service=object(),
            user=SimpleNamespace(email="operator@example.test", user_id="operator"),
        )
    )


def test_partner_create_refuses_a_project_outside_its_shared_cluster_bound(monkeypatch):
    async def forbidden_client(*args, **kwargs):
        pytest.fail("a foreign Partner project opened an LXD client")

    monkeypatch.setattr(instance_snapshots, "_client", forbidden_client)
    allowed_token = org_scope._allowed_clusters_var.set({HOST})
    projects_token = org_scope._visible_projects_var.set({HOST: {PROJECT}})
    acl_token = org_scope._tenant_acl_var.set(None)
    try:
        with pytest.raises(HTTPException) as exc:
            _create_direct(FOREIGN_PROJECT)
    finally:
        org_scope._tenant_acl_var.reset(acl_token)
        org_scope._visible_projects_var.reset(projects_token)
        org_scope._allowed_clusters_var.reset(allowed_token)

    assert exc.value.status_code == 403
    assert exc.value.detail == "Access denied to this project"


def test_tenant_snapshot_only_grant_is_denied_before_lxd_io(monkeypatch):
    async def forbidden_client(*args, **kwargs):
        pytest.fail("a Tenant without view permission opened an LXD client")

    monkeypatch.setattr(instance_snapshots, "_client", forbidden_client)
    settings = get_settings()
    original_flag = settings.snapshot_tenant_enforcement_enabled
    object.__setattr__(settings, "snapshot_tenant_enforcement_enabled", True)
    allowed_token = org_scope._allowed_clusters_var.set({HOST})
    projects_token = org_scope._visible_projects_var.set({HOST: {PROJECT}})
    acl_token = org_scope._tenant_acl_var.set({HOST: {(PROJECT, "*"): {"snapshot"}}})
    try:
        with pytest.raises(HTTPException) as exc:
            _create_direct(PROJECT)
    finally:
        org_scope._tenant_acl_var.reset(acl_token)
        org_scope._visible_projects_var.reset(projects_token)
        org_scope._allowed_clusters_var.reset(allowed_token)
        object.__setattr__(settings, "snapshot_tenant_enforcement_enabled", original_flag)

    assert exc.value.status_code == 403
    assert "no 'view' permission" in exc.value.detail


def test_terminal_create_failure_is_audited_without_webhook_or_cache_invalidation(
    create_side_effects,
):
    node = FakeInstanceNode()
    node.operation_status = "Failure"
    node.operation_err = "storage pool is full"

    response = _client(node).post(
        f"/instances/{INSTANCE}/snapshots",
        json={"host": HOST, "project": PROJECT, "label": "doomed"},
    )

    assert response.status_code == 502, response.text
    assert response.json()["detail"] == {
        "error": "snapshot_terminal_failure",
        "message": "The cluster refused the snapshot: storage pool is full",
    }
    failures = [
        row
        for row in create_side_effects.audits
        if row.get("action") == "instance.snapshot.create" and row.get("result") == "failure"
    ]
    assert len(failures) == 1
    assert failures[0]["error_message"] == "storage pool is full"
    assert create_side_effects.webhooks == []
    assert create_side_effects.invalidations == []


def test_confirmed_create_invalidates_the_fleet_cache(create_side_effects):
    response = _client(FakeInstanceNode()).post(
        f"/instances/{INSTANCE}/snapshots",
        json={"host": HOST, "project": PROJECT, "label": "confirmed"},
    )

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "succeeded"
    assert create_side_effects.invalidations == [
        "snapshots_fleet",
        "storage_pool_resources",
    ]
