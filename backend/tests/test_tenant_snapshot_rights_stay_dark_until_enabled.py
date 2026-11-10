"""A tenant gets no snapshot rights until SNAPSHOT_TENANT_ENFORCEMENT_ENABLED is set.

Existing policy rules, including those pushed by OrcaHub's tenant-access sync, may already carry
the `snapshot` permission. Enforcing it under TENANT_PERM_ENFORCEMENT_ENABLED would turn every
such grant into a live capability at once; a separate flag makes enabling it an explicit step.

Admin and Partner carry no tenant ACL and are unaffected either way.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests._snapshot_fakes import HOST, INSTANCE, PROJECT, FakeClient, FakeInstanceNode, service

TENANT_ACL = {HOST: {(PROJECT, "*"): {"view", "operate", "snapshot"}}}


@pytest.fixture(autouse=True)
def _no_webhook(monkeypatch):
    from app.routers import instance_snapshots

    monkeypatch.setattr(instance_snapshots, "schedule_orcahub_event", lambda *a, **k: None)


def _client(svc, *, acl, flag_on: bool) -> TestClient:
    from app.core import org_scope
    from app.core.config import get_settings
    from app.core.org_scope import org_scope_dependency
    from app.core.rbac import get_current_user
    from app.routers import instance_snapshots
    from app.services.lxd_service import get_lxd_service

    app = FastAPI()
    app.include_router(instance_snapshots.router)
    app.dependency_overrides[get_lxd_service] = lambda: svc
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None

    # Settings are lru_cache'd, so the flag is flipped on the cached instance.
    settings = get_settings()
    original_flag = settings.snapshot_tenant_enforcement_enabled
    object.__setattr__(settings, "snapshot_tenant_enforcement_enabled", flag_on)
    token = org_scope._tenant_acl_var.set(acl)

    client = TestClient(app, raise_server_exceptions=False)
    client._cleanup = lambda: (
        org_scope._tenant_acl_var.reset(token),
        object.__setattr__(settings, "snapshot_tenant_enforcement_enabled", original_flag),
    )
    return client


def _create(client):
    return client.post(
        f"/instances/{INSTANCE}/snapshots",
        json={"host": HOST, "project": PROJECT, "label": "attempt"},
    )


def test_a_tenant_with_the_grant_is_refused_while_the_flag_is_off():
    node = FakeInstanceNode()
    client = _client(service(FakeClient(node)), acl=TENANT_ACL, flag_on=False)
    try:
        response = _create(client)
        assert response.status_code == 403, response.text
        assert not node.recorded, "a refused create must not have reached the daemon"
    finally:
        client._cleanup()


def test_the_same_tenant_is_allowed_once_the_flag_is_on():
    node = FakeInstanceNode()
    client = _client(service(FakeClient(node)), acl=TENANT_ACL, flag_on=True)
    try:
        assert _create(client).status_code == 201
        assert [c for c in node.recorded if c[0] == "create_snapshot"]
    finally:
        client._cleanup()


def test_a_tenant_without_the_grant_is_refused_even_with_the_flag_on():
    node = FakeInstanceNode()
    acl = {HOST: {(PROJECT, "*"): {"view", "operate"}}}
    client = _client(service(FakeClient(node)), acl=acl, flag_on=True)
    try:
        assert _create(client).status_code == 403
        assert not node.recorded
    finally:
        client._cleanup()


def test_restore_additionally_requires_operate():
    # A rule may grant snapshot without operate. Restore stops the instance and discards
    # writes, so it is a lifecycle action and needs the lifecycle permission too.
    node = FakeInstanceNode(snapshots=[{"name": "ops-t-20260908T000000Z"}])
    acl = {HOST: {(PROJECT, "*"): {"view", "snapshot"}}}
    client = _client(service(FakeClient(node)), acl=acl, flag_on=True)
    try:
        response = client.post(
            f"/instances/{INSTANCE}/snapshots/ops-t-20260908T000000Z/restore",
            json={"host": HOST, "project": PROJECT, "confirm_name": INSTANCE},
        )
        assert response.status_code == 403, response.text
        assert not [c for c in node.recorded if c[0] == "put_instance"]
    finally:
        client._cleanup()


def test_an_admin_is_unaffected_by_the_flag():
    node = FakeInstanceNode()
    client = _client(service(FakeClient(node)), acl=None, flag_on=False)
    try:
        assert _create(client).status_code == 201
    finally:
        client._cleanup()
