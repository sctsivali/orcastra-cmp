"""The picker reads the /nodes fan-out instead of starting its own.

Six create forms open a node picker. A resource fan-out per open would make the endpoint a load
generator, and on a fleet where most clusters are down each open would pay a connect timeout per
dead cluster. When the node-status page or the metrics prewarm has already run the fan-out, this
reads it from Redis for the cost of one GET.

A picker row needs a name and a status, so the payload carries no hardware inventory.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.cache import NODES_AVAILABILITY_CACHE_KEY, NODES_STATUS_CACHE_KEY
from app.models.cluster_status import ClusterStatus

NODES_BODY = {
    "all_connected": False,
    "nodes": [
        {
            "name": "nuc2", "endpoint": "https://nuc2:8443", "connected": True,
            "reachable": True, "node_type": "microcloud", "display_name": "NUC 2",
            "server_version": "5.21", "api_version": "1.0", "auth": "trusted",
            "error": None, "tags": ["prod"],
            "environment": {"kernel": "6.8.0"},
            "resources": {"cpu_total": 32, "cpu_used": 4, "memory_total": 1, "memory_used": 0,
                          "gpu_total": 2, "gpu_cards_total": 4, "gpu_cards": [{"pci": "00:1f"}]},
        },
        {
            "name": "blizzard", "endpoint": "https://93.184.215.14:8443", "connected": False,
            "reachable": False, "node_type": None, "display_name": None,
            "server_version": None, "api_version": None, "auth": None,
            "error": "Cannot connect to LXD endpoint. Check if server is running.",
            "tags": [], "environment": None, "resources": None,
        },
        {
            "name": "center1-alpha", "endpoint": "https://center1:8443", "connected": False,
            "reachable": True, "node_type": None, "display_name": None,
            "server_version": None, "api_version": None, "auth": "untrusted",
            "error": "Certificate not trusted", "tags": [], "environment": None,
            "resources": None,
        },
    ],
}


class _Cache:
    def __init__(self):
        self.writes = {}

    def get(self, namespace, key):
        if NODES_STATUS_CACHE_KEY in key:
            return NODES_BODY
        return None

    def set(self, namespace, key, value, ttl=None):
        self.writes[key] = (value, ttl)

    def delete_pattern(self, *args, **kwargs):
        return None


class _Service:
    def get_node_names(self):
        raise AssertionError("the projection path must not ask for the cluster list")


@pytest.fixture
def client(monkeypatch):
    from app.routers import nodes as nodes_module
    from app.services.lxd import status as lxd_status
    from app.services.lxd_service import get_lxd_service
    from app.core.rbac import get_current_user
    from app.core.org_scope import org_scope_dependency

    cache = _Cache()
    # Patched on the service: resolve_availability owns the cache read for both
    # /nodes/availability and /integrations/available-clusters.
    monkeypatch.setattr(lxd_status, "get_cache_manager", lambda: cache)

    def _explode(*args, **kwargs):
        raise AssertionError("a warm /nodes cache must not trigger a probe sweep")

    monkeypatch.setattr(lxd_status, "probe_clusters", _explode)

    app = FastAPI()
    app.include_router(nodes_module.router)
    app.dependency_overrides[get_lxd_service] = _Service
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    return TestClient(app, raise_server_exceptions=False), cache


def test_a_warm_nodes_cache_answers_without_any_probe(client):
    tc, _ = client
    response = tc.get("/nodes/availability")
    assert response.status_code == 200, response.text
    assert response.json()["source"] == "nodes_cache"


def test_the_projection_preserves_the_three_states(client):
    tc, _ = client
    by_name = {r["name"]: r for r in tc.get("/nodes/availability").json()["clusters"]}
    assert by_name["nuc2"]["status"] == ClusterStatus.ONLINE.value
    assert by_name["blizzard"]["status"] == ClusterStatus.OFFLINE.value
    # Answered but refused the client certificate: a different problem with a different fix.
    assert by_name["center1-alpha"]["status"] == ClusterStatus.NOT_AUTHORIZED.value


def test_the_payload_carries_no_hardware_inventory(client):
    """A dropdown row must not ship every cluster's CPU, memory and GPU detail to every
    role that can open a create form."""
    tc, _ = client
    body = tc.get("/nodes/availability").text
    for leaked in ("resources", "gpu_cards", "environment", "cpu_total", "tags"):
        assert leaked not in body, f"{leaked} leaked into the picker payload"


def test_display_name_falls_back_to_the_id(client):
    tc, _ = client
    by_name = {r["name"]: r for r in tc.get("/nodes/availability").json()["clusters"]}
    assert by_name["nuc2"]["display_name"] == "NUC 2"
    assert by_name["blizzard"]["display_name"] == "blizzard"


def test_the_projection_is_cached_so_the_next_read_is_one_get(client):
    tc, cache = client
    tc.get("/nodes/availability")
    assert any(NODES_AVAILABILITY_CACHE_KEY in k for k in cache.writes)
