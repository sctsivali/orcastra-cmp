"""Legacy Copy Instance admission and low-level copy-payload regressions.

The public endpoint is a compatibility adapter for the durable migration engine. It must
authorize both endpoints and may not fall back to a request-scoped daemon copy or migration when
rollout is disabled. The low-level same-cluster helper is covered directly because the durable
worker relies on its exact LXD payload shape.
"""

import inspect

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.routers import instance_mobility
from app.services.lxd import mobility
from tests._snapshot_fakes import (
    HOST,
    INSTANCE,
    OTHER_HOST,
    PROJECT,
    FakeClient,
    FakeInstanceNode,
    service,
)


class _CopyableInstanceNode(FakeInstanceNode):
    """Add the instance type required by the mobility helper to the shared fake."""

    def get(self, params=None):
        response = super().get(params=params)
        response._payload["metadata"]["type"] = "container"
        return response


@pytest.fixture(autouse=True)
def _durable_copy_disabled_by_default():
    """Keep compatibility admission deterministic even when another test mutates settings."""
    settings = get_settings()
    old_center = settings.migration_center_enabled
    old_copy = settings.migration_durable_copy_enabled
    object.__setattr__(settings, "migration_center_enabled", False)
    object.__setattr__(settings, "migration_durable_copy_enabled", False)
    try:
        yield
    finally:
        object.__setattr__(settings, "migration_center_enabled", old_center)
        object.__setattr__(settings, "migration_durable_copy_enabled", old_copy)


def _client(svc, *, allowed_clusters=None) -> TestClient:
    from app.core import org_scope
    from app.core.database import get_db
    from app.core.org_scope import org_scope_dependency
    from app.core.rbac import get_current_user
    from app.services.lxd_service import get_lxd_service

    app = FastAPI()
    app.include_router(instance_mobility.router)
    app.dependency_overrides[get_lxd_service] = lambda: svc
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None

    async def test_database():
        yield object()

    app.dependency_overrides[get_db] = test_database

    token = org_scope._allowed_clusters_var.set(allowed_clusters)
    client = TestClient(app, raise_server_exceptions=False)
    client._cleanup = lambda: org_scope._allowed_clusters_var.reset(token)
    return client


def _copy(client, **overrides):
    body = {
        "host": HOST,
        "project": PROJECT,
        "target_host": HOST,
        "target_project": "scratch",
        "target_name": "ct-qa-copy-0002",
    }
    body.update(overrides)
    return client.post(f"/instances/{INSTANCE}/copy", json=body)


def _body(**overrides) -> instance_mobility.MoveRequest:
    values = {
        "host": HOST,
        "project": PROJECT,
        "target_host": HOST,
        "target_project": "scratch",
        "target_name": "ct-qa-copy-0002",
    }
    values.update(overrides)
    return instance_mobility.MoveRequest(**values)


def test_a_destination_outside_the_callers_scope_is_refused():
    node = FakeInstanceNode()
    fake = FakeClient(node)
    svc = service(fake, hosts=(HOST, OTHER_HOST))
    client = _client(svc, allowed_clusters={HOST})
    try:
        response = _copy(client, target_host=OTHER_HOST)
        assert response.status_code == 403, response.text
        assert not fake.api.instances.created
        assert not node.recorded
    finally:
        client.close()
        client._cleanup()


def test_a_source_outside_the_callers_scope_is_refused():
    node = FakeInstanceNode()
    fake = FakeClient(node)
    svc = service(fake, hosts=(HOST, OTHER_HOST))
    client = _client(svc, allowed_clusters={OTHER_HOST})
    try:
        response = _copy(client, host=HOST, target_host=OTHER_HOST)
        assert response.status_code == 403, response.text
        assert not fake.api.instances.created
        assert not node.recorded
    finally:
        client.close()
        client._cleanup()


def test_disabled_legacy_copy_fails_before_any_daemon_read_or_write():
    node = FakeInstanceNode()
    fake = FakeClient(node)
    client = _client(service(fake, hosts=(HOST,)), allowed_clusters={HOST})
    try:
        response = _copy(client)
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == {
            "error": "durable_copy_admission_disabled",
            "message": "Durable Copy Instance admission is not enabled.",
            "remediation": (
                "Use Migration Center preflight and enable the reviewed durable-copy "
                "rollout stage before submitting a copy."
            ),
        }
        assert not node.recorded
        assert not fake.api.instances.created
    finally:
        client.close()
        client._cleanup()


def test_the_router_has_no_direct_or_streaming_copy_fallback():
    """Only the durable adapter may execute a request accepted by the legacy route."""
    assert "force_transport" not in instance_mobility.MoveRequest.model_fields
    for gone in ("_stream_copy", "_pipe_backup_to_target", "_cross_cluster", "_settle_copy"):
        assert not hasattr(instance_mobility, gone), f"{gone} is reachable again"

    handler_source = inspect.getsource(instance_mobility.copy_instance)
    assert "_durable_copy" in handler_source
    assert "copy_within_cluster" not in handler_source
    assert "begin_source_migration" not in handler_source
    assert "TRANSPORT_STREAM" not in inspect.getsource(instance_mobility)


def test_same_cluster_helper_names_both_projects_and_target_member():
    fake = FakeClient(_CopyableInstanceNode())

    operation = mobility.copy_within_cluster(
        fake,
        source_name=INSTANCE,
        source_project=PROJECT,
        target_name="ct-qa-copy-0002",
        target_project="scratch",
        target_member="member-2",
    )

    assert operation == "/1.0/operations/op-copy"
    body, params = fake.api.instances.created[0]
    assert body["source"] == {
        "type": "copy",
        "source": INSTANCE,
        "project": PROJECT,
    }
    assert params == {"project": "scratch", "target": "member-2"}


def test_same_cluster_helper_reproduces_config_devices_and_profiles():
    node = _CopyableInstanceNode(
        config={
            "limits.cpu": "1",
            "limits.memory": "256MiB",
            "volatile.eth0.hwaddr": "00:16:3e:aa:bb:cc",
        },
    )
    node.devices = {"root": {"path": "/", "type": "disk", "pool": "default", "size": "2GiB"}}
    node.profiles = ["default", "restricted"]
    fake = FakeClient(node)

    mobility.copy_within_cluster(
        fake,
        source_name=INSTANCE,
        source_project=PROJECT,
        target_name="ct-qa-copy-0002",
        target_project="scratch",
    )

    body, _params = fake.api.instances.created[0]
    assert body["type"] == "container"
    assert body["config"]["limits.cpu"] == "1"
    assert body["config"]["limits.memory"] == "256MiB"
    assert "volatile.eth0.hwaddr" not in body["config"]
    assert body["devices"]["root"]["size"] == "2GiB"
    assert body["profiles"] == ["default", "restricted"]


def test_a_contract_copy_names_the_architecture_and_keeps_a_preserved_mac():
    """LXD 5.21 refuses an explicit-config copy with no architecture when the source lives on the
    member answering the request ("Architecture is not supported: \"\"")."""
    fake = FakeClient(_CopyableInstanceNode())
    definition = {
        "type": "container",
        "architecture": "x86_64",
        "profiles": ["default"],
        "config": {"user.orcastra.migration-job": "job-1", "volatile.eth0.hwaddr": "00:16:3e:aa:bb:cc"},
        "devices": {},
    }

    mobility.copy_within_cluster(
        fake,
        source_name=INSTANCE,
        source_project=PROJECT,
        target_name=INSTANCE,
        target_project="scratch",
        definition=definition,
    )

    body, _params = fake.api.instances.created[0]
    assert body["architecture"] == "x86_64"
    assert body["config"]["volatile.eth0.hwaddr"] == "00:16:3e:aa:bb:cc"


def test_target_pool_overrides_only_the_effective_root_disk():
    node = _CopyableInstanceNode(config={"limits.cpu": "1"})
    node.devices = {
        "os": {"path": "/", "type": "disk", "pool": "default", "size": "2GiB"},
        "data": {"path": "/srv", "type": "disk", "pool": "bulk", "source": "data"},
    }
    fake = FakeClient(node)

    mobility.copy_within_cluster(
        fake,
        source_name=INSTANCE,
        source_project=PROJECT,
        target_name="ct-qa-copy-0002",
        target_project="scratch",
        target_pool="nvme",
    )

    body, _params = fake.api.instances.created[0]
    assert body["devices"]["os"] == {
        "path": "/",
        "type": "disk",
        "pool": "nvme",
        "size": "2GiB",
    }
    assert body["devices"]["data"]["pool"] == "bulk"
    assert "root" not in body["devices"]


def test_billing_gate_refuses_an_unacknowledged_external_schedule(monkeypatch):
    audits = []
    monkeypatch.setattr(
        instance_mobility,
        "_audit_move",
        lambda *args, **kwargs: audits.append((args, kwargs)),
    )

    with pytest.raises(HTTPException) as exc:
        instance_mobility._gate_copy(
            {"externally_managed": True, "schedule": "@hourly", "expiry": "1d"},
            INSTANCE,
            _body(),
        )

    assert exc.value.status_code == 403
    assert exc.value.detail["error"] == "billing_impact_acknowledgement_required"
    assert exc.value.detail["acknowledge_field"] == "acknowledge_billing_impact"
    assert audits[0][1]["result"] == "denied"


def test_billing_gate_returns_an_acknowledged_justification():
    reason = "Capacity rebalance approved by the operator"
    result = instance_mobility._gate_copy(
        {"externally_managed": True, "schedule": "@hourly", "expiry": "1d"},
        INSTANCE,
        _body(acknowledge_billing_impact=True, reason=reason),
    )

    assert result == reason


def test_billing_gate_does_not_require_acknowledgement_without_external_management():
    result = instance_mobility._gate_copy(
        {"externally_managed": False},
        INSTANCE,
        _body(),
    )

    assert result is None
