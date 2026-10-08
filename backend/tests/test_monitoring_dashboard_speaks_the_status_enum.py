"""The monitoring dashboard must not invent its own status vocabulary.

/monitoring/dashboard used to decide a cluster's state by substring-matching the text of its
error, and emitted "pending_trust", a value that is in no enum. A host that was simply powered
off came back labelled a certificate problem, which sends an operator to rotate a certificate on
a machine that is not answering. models/cluster_status.py was written to end exactly that, and
/clusters was fixed at the time while this endpoint was left behind.

These tests pin the endpoint to the shared vocabulary, and pin it against the same two failures
that test_a_dead_cluster_is_not_labelled_pending_trust.py pins for /clusters: a host that never
answered, and one that answered and refused our certificate.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models.cluster_status import ClusterStatus
from app.models.schemas import NodeResources, NodeStatus
from app.services.lxd.instance import FleetInventory

DEAD = "blitzkrieg"
UNTRUSTED = "central1-argon"
ONLINE = "nuc2"


def _nodes():
    return [
        # never answered: no auth was ever read, and the breaker recorded an offline failure
        NodeStatus(
            name=DEAD,
            endpoint="https://dead:8443",
            connected=False,
            reachable=False,
            error="Cannot connect to LXD endpoint.",
        ),
        # answered and refused us
        NodeStatus(
            name=UNTRUSTED,
            endpoint="https://untrusted:8443",
            connected=False,
            reachable=True,
            error="Certificate not trusted by hypervisor.",
        ),
        # note: resources are not decoration here. resources.py only sets connected=True once
        # it has actually read them, and the dashboard loop skips a connected node that has
        # none, so a resourceless "online" node would not be a realistic fixture.
        NodeStatus(
            name=ONLINE,
            endpoint="https://nuc2:8443",
            connected=True,
            reachable=True,
            auth="trusted",
            resources=NodeResources(
                cpu_total=8,
                cpu_used=2,
                memory_total=16 * 1024**3,
                memory_used=4 * 1024**3,
                storage_total=512 * 1024**3,
                storage_available=256 * 1024**3,
            ),
        ),
    ]


class _MetadataStore:
    """Registration recorded the hypervisor family. An untrusted node never reports its own."""

    def get_all_node_metadata(self):
        return {UNTRUSTED: {"type": "incus"}}


class _Cache:
    def get(self, *args, **kwargs):
        return None

    def set(self, *args, **kwargs):
        return None


class _Service:
    async def read_fleet_inventory(self):
        return FleetInventory([], [], [], {})

    def get_node_names(self):
        return [node.name for node in _nodes()]

    def check_node_status(self, name):
        return next(node for node in _nodes() if node.name == name)


@pytest.fixture
def client(monkeypatch):
    from app.routers import monitoring as module
    from app.core.rbac import get_current_user
    from app.core.org_scope import org_scope_dependency

    monkeypatch.setattr(module, "get_cache_manager", lambda: _Cache())
    monkeypatch.setattr(module, "get_node_metadata_store", lambda: _MetadataStore())
    monkeypatch.setattr(module, "get_scope_cache_key", lambda key: key)
    monkeypatch.setattr(module, "check_tenant_permission", lambda *a, **k: True)
    monkeypatch.setattr(module, "tenant_acl_active", lambda *a, **k: False)

    app = FastAPI()
    app.include_router(module.router)
    app.dependency_overrides[module.get_lxd_service] = lambda: _Service()
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def _by_name(client):
    body = client.get("/monitoring/dashboard").json()
    assert "nodes" in body, body
    return {n["node_name"]: n for n in body["nodes"]}


def test_every_status_is_a_real_enum_member(client):
    """The regression that mattered: "pending_trust" is in no enum and must never be emitted."""
    allowed = {s.value for s in ClusterStatus}
    for name, node in _by_name(client).items():
        assert node["cluster_status"] in allowed, (name, node["cluster_status"])


def test_a_cluster_that_never_answered_is_offline(client):
    assert _by_name(client)[DEAD]["cluster_status"] == ClusterStatus.OFFLINE.value


def test_a_cluster_that_answered_and_refused_us_is_not_authorized(client):
    assert _by_name(client)[UNTRUSTED]["cluster_status"] == ClusterStatus.NOT_AUTHORIZED.value


def test_the_two_failures_are_not_the_same_status(client):
    """The whole point. Before the fix both read pending_trust."""
    nodes = _by_name(client)
    assert nodes[DEAD]["cluster_status"] != nodes[UNTRUSTED]["cluster_status"]


def test_only_the_untrusted_cluster_gets_the_trust_hint(client):
    """The CLI hint on a dead host is what sent operators to the wrong machine."""
    nodes = _by_name(client)
    assert "config trust" in nodes[UNTRUSTED]["status_message"]
    assert "config trust" not in nodes[DEAD]["status_message"]


def test_an_unreachable_cluster_still_says_why(client):
    assert _by_name(client)[DEAD]["status_message"]


def test_a_connected_cluster_is_online(client):
    node = _by_name(client)[ONLINE]
    assert node["cluster_status"] == ClusterStatus.ONLINE.value
    assert node["is_online"] is True


def test_the_trust_hint_names_the_right_cli_for_an_incus_cluster(client):
    """resources.py leaves node_type None for exactly the untrusted node, so the hint used to
    fall back to lxc and told an Incus operator to run a command that does not exist."""
    message = _by_name(client)[UNTRUSTED]["status_message"]
    assert "incus config trust add-certificate" in message, message
    assert "lxc config trust" not in message


def test_the_trust_hint_is_not_said_twice(client):
    """derive_status already ends its untrusted message with a generic "Add trust on the
    cluster.", so appending the concrete command produced that instruction twice in one line."""
    message = _by_name(client)[UNTRUSTED]["status_message"]
    assert message.lower().count("on the cluster") == 1, message
    assert "Add trust on the cluster" not in message, message
