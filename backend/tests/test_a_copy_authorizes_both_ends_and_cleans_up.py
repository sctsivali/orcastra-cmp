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


def test_a_streamed_copy_deletes_its_backup_even_when_the_transfer_fails(monkeypatch):
    from app.routers import instance_mobility

    node = FakeInstanceNode()
    fake = FakeClient(node)

    def _boom(*args, **kwargs):
        raise RuntimeError("the destination refused the import")

    monkeypatch.setattr(instance_mobility, "_pipe_backup_to_target", _boom)

    client = _client(service(fake, hosts=(HOST, OTHER_HOST)), allowed_clusters=None)
    try:
        response = _copy(client, target_host=OTHER_HOST, force_transport="stream")
        assert response.status_code >= 400, response.text
    finally:
        client._cleanup()

    created = [c for c in node.recorded if c[0] == "create_backup"]
    deleted = [c for c in node.recorded if c[0] == "delete_backup"]
    assert created, "the streaming transport should have created a backup"
    assert deleted, "a failed transfer must still delete the backup it made"
    assert deleted[0][1] == created[0][1]["name"], "it must delete the backup it created"


def test_an_oversized_instance_is_refused_before_a_backup_is_created():
    from app.services.lxd import mobility

    node = FakeInstanceNode()
    node.state_usage = mobility.STREAM_MAX_BYTES + 1
    fake = FakeClient(node)

    client = _client(service(fake, hosts=(HOST, OTHER_HOST)), allowed_clusters=None)
    try:
        response = _copy(client, target_host=OTHER_HOST, force_transport="stream")
        assert response.status_code == 409, response.text
    finally:
        client._cleanup()

    assert not [c for c in node.recorded if c[0] == "create_backup"], (
        "the size ceiling must be checked before consuming pool space"
    )


def test_force_transport_only_accepts_the_two_real_transports():
    node = FakeInstanceNode()
    client = _client(service(FakeClient(node), hosts=(HOST,)), allowed_clusters=None)
    try:
        assert _copy(client, force_transport="carrier-pigeon").status_code == 422
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
