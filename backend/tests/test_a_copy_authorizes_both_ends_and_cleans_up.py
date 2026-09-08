"""Copying a workload checks the destination as well as the source, and never strands an export.

Two failures this guards, both of which have precedent in this system:

* Checking only the source would let a partner read out of a cluster they own and write into one
  they do not. The destination is where the data lands, so it is the half that matters most.
* The streaming transport creates a real backup object on the source's storage pool. It is
  invisible in every UI and consumes the pool until someone notices it is full. Stranded image
  imports are a recurring bug class here, so the cleanup is asserted on the failure path, not
  only the happy one.
"""

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from tests._snapshot_fakes import (
    HOST,
    INSTANCE,
    OTHER_HOST,
    PROJECT,
    FakeClient,
    FakeInstanceNode,
    service,
)


@pytest.fixture(autouse=True)
def _no_webhook(monkeypatch):
    from app.routers import instance_mobility

    monkeypatch.setattr(instance_mobility, "schedule_orcahub_event", lambda *a, **k: None)


def _client(svc, *, allowed_clusters=None) -> TestClient:
    from app.core import org_scope
    from app.core.org_scope import org_scope_dependency
    from app.core.rbac import get_current_user
    from app.routers import instance_mobility
    from app.services.lxd_service import get_lxd_service

    app = FastAPI()
    app.include_router(instance_mobility.router)
    app.dependency_overrides[get_lxd_service] = lambda: svc
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None

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


def test_a_destination_outside_the_callers_scope_is_refused():
    node = FakeInstanceNode()
    svc = service(FakeClient(node), hosts=(HOST, OTHER_HOST))
    client = _client(svc, allowed_clusters={HOST})
    try:
        response = _copy(client, target_host=OTHER_HOST)
        assert response.status_code == 403, response.text
        assert not svc.connection_manager._client.api.instances.created, (
            "a refused copy must not have created anything"
        )
    finally:
        client._cleanup()


def test_a_source_outside_the_callers_scope_is_refused():
    node = FakeInstanceNode()
    svc = service(FakeClient(node), hosts=(HOST, OTHER_HOST))
    client = _client(svc, allowed_clusters={OTHER_HOST})
    try:
        assert _copy(client, host=HOST, target_host=OTHER_HOST).status_code == 403
    finally:
        client._cleanup()


def test_a_same_cluster_copy_names_the_source_project_and_targets_the_destination():
    node = FakeInstanceNode()
    fake = FakeClient(node)
    client = _client(service(fake, hosts=(HOST,)), allowed_clusters={HOST})
    try:
        response = _copy(client)
        assert response.status_code == 202, response.text
        assert response.json()["transport"] == "copy"

        created = fake.api.instances.created
        assert len(created) == 1
        body, params = created[0]
        assert body["source"]["type"] == "copy"
        assert body["source"]["project"] == PROJECT, "the source project must travel in the body"
        assert params["project"] == "scratch", "the destination project is a query parameter"
    finally:
        client._cleanup()


def test_the_streaming_transport_is_not_reachable_from_the_router():
    """The stream path has never executed and cannot work as written, so nothing may reach it.

    It stayed in the service layer as a starting point for a queued transfer job. What must not
    come back is a request-scoped route into it: it held a worker from the pool that also serves
    console and terminal traffic for up to an hour, and it never awaited the backup operation it
    read from. This asserts the absence, because a reachable half-built data path is worse than
    no data path.
    """
    import inspect

    from app.routers import instance_mobility

    assert "force_transport" not in instance_mobility.MoveRequest.model_fields, (
        "force_transport is what let any Partner reach the stream path over the raw API"
    )
    for gone in ("_stream_copy", "_pipe_backup_to_target"):
        assert not hasattr(instance_mobility, gone), f"{gone} is reachable again"

    source = inspect.getsource(instance_mobility)
    assert "TRANSPORT_STREAM" not in source, "the router must not name the stream transport"


def test_an_unknown_transport_hint_cannot_select_a_transport():
    """An extra field is ignored rather than honoured, so the server still chooses."""
    node = FakeInstanceNode()
    client = _client(service(FakeClient(node), hosts=(HOST,)), allowed_clusters=None)
    try:
        response = _copy(client, force_transport="stream")
        assert response.status_code == 202, response.text
        assert response.json()["transport"] == "copy", (
            "a same-cluster copy must stay on the copy transport whatever the body asks for"
        )
    finally:
        client._cleanup()


def test_a_copy_reproduces_the_sources_config_and_devices():
    """A copy must declare the config and devices it is reproducing.

    Every customer project on this fleet carries limits.cpu, limits.memory and limits.disk. LXD
    checks a destination project's quota BEFORE it resolves anything from the copy source, so a
    body that omits them is refused with "instance has no limits.cpu config", which points at
    the copy rather than at the project it landed in. Proven live: the same request succeeds the
    moment config and devices travel with it.

    volatile.* is excluded on purpose. It carries generated identity such as MAC addresses, and
    reproducing it would put two instances with the same hardware identity on one network.
    """
    node = FakeInstanceNode(
        config={
            "limits.cpu": "1",
            "limits.memory": "256MiB",
            "volatile.eth0.hwaddr": "00:16:3e:aa:bb:cc",
        },
    )
    node.devices = {"root": {"path": "/", "type": "disk", "pool": "default", "size": "2GiB"}}
    node.profiles = ["default"]
    fake = FakeClient(node)

    client = _client(service(fake, hosts=(HOST,)), allowed_clusters={HOST})
    try:
        assert _copy(client).status_code == 202
    finally:
        client._cleanup()

    body, _params = fake.api.instances.created[0]
    assert body["config"]["limits.cpu"] == "1", "the copy must declare the source's limits"
    assert "volatile.eth0.hwaddr" not in body["config"], (
        "volatile identity must not be reproduced onto a second instance"
    )
    assert body["devices"]["root"]["size"] == "2GiB", "the root device must travel with the copy"
    assert body["profiles"] == ["default"]


def test_a_target_pool_overrides_only_the_root_pool():
    node = FakeInstanceNode(config={"limits.cpu": "1"})
    node.devices = {"root": {"path": "/", "type": "disk", "pool": "default", "size": "2GiB"}}
    node.profiles = ["default"]
    fake = FakeClient(node)

    client = _client(service(fake, hosts=(HOST,)), allowed_clusters={HOST})
    try:
        assert _copy(client, target_pool="nvme").status_code == 202
    finally:
        client._cleanup()

    body, _ = fake.api.instances.created[0]
    assert body["devices"]["root"]["pool"] == "nvme"
    assert body["devices"]["root"]["size"] == "2GiB", "overriding the pool must not drop the size"


# ── the billing gate, and the operation actually settling ──────────────────────────────────


def test_a_copy_of_a_billed_instance_is_refused_without_an_acknowledgement():
    """A copy duplicates a workload whose storage another product meters and bills."""
    node = FakeInstanceNode(config={"snapshots.schedule": "@hourly"})
    client = _client(service(FakeClient(node), hosts=(HOST,)), allowed_clusters=None)
    try:
        response = _copy(client)
        assert response.status_code == 403, response.text
        detail = response.json()["detail"]
        assert detail["error"] == "billing_impact_acknowledgement_required"
        assert detail["acknowledge_field"] == "acknowledge_billing_impact"
    finally:
        client._cleanup()

    assert not [c for c in node.recorded if c[0] == "post_instance"], (
        "a refused copy must not reach the daemon"
    )


def test_an_acknowledged_copy_of_a_billed_instance_proceeds_and_records_the_override(monkeypatch):
    from app.core import snapshot_billing_gate

    rows = []
    monkeypatch.setattr(
        snapshot_billing_gate.activity_logger, "log_activity", lambda **kw: rows.append(kw)
    )

    node = FakeInstanceNode(config={"snapshots.schedule": "@hourly"})
    client = _client(service(FakeClient(node), hosts=(HOST,)), allowed_clusters=None)
    try:
        response = _copy(
            client, acknowledge_billing_impact=True, reason="moving off a failing node"
        )
        assert response.status_code == 202, response.text
    finally:
        client._cleanup()

    overrides = [r for r in rows if r["action"] == "instance.snapshot.billing_override"]
    assert overrides, f"a forced copy must be separately alertable: {rows}"
    assert overrides[0]["details"]["overridden_action"] == "copy"
    assert overrides[0]["details"]["reason"] == "moving off a failing node"


def test_an_acknowledgement_without_a_reason_is_still_refused():
    node = FakeInstanceNode(config={"snapshots.schedule": "@hourly"})
    client = _client(service(FakeClient(node), hosts=(HOST,)), allowed_clusters=None)
    try:
        assert _copy(client, acknowledge_billing_impact=True).status_code == 403
    finally:
        client._cleanup()


def test_an_unscheduled_instance_needs_no_acknowledgement():
    node = FakeInstanceNode()
    client = _client(service(FakeClient(node), hosts=(HOST,)), allowed_clusters=None)
    try:
        assert _copy(client).status_code == 202
    finally:
        client._cleanup()


def test_a_copy_the_cluster_fails_after_accepting_is_not_reported_as_started():
    """The 202 says the request was understood. Everything that can go wrong happens after it."""
    node = FakeInstanceNode()
    node.operation_status = "Failure"
    node.operation_err = "Failed to connect to source: connection refused"

    client = _client(service(FakeClient(node), hosts=(HOST,)), allowed_clusters=None)
    try:
        response = _copy(client)
        assert response.status_code == 502, response.text
        detail = response.json()["detail"]
        assert "connection refused" in detail
        # The one failure an operator cannot read off the raw message.
        assert "open a connection" in detail, detail
    finally:
        client._cleanup()


def test_a_settled_copy_says_it_finished_rather_than_that_it_started():
    node = FakeInstanceNode()
    client = _client(service(FakeClient(node), hosts=(HOST,)), allowed_clusters=None)
    try:
        response = _copy(client)
        assert response.status_code == 202, response.text
        assert "copied to" in response.json()["message"]
    finally:
        client._cleanup()
