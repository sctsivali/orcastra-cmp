"""A usage percentage is None when nothing reported the total it is a share of.

With every cluster offline the totals are zero, and a 0.0 share would draw "0.0%" over
"0 B / 0 B", the reading of an idle fleet, for a fleet that cannot be seen. The same applies to
each offline cluster row and to an online cluster with no storage pool.

The dashboard also counts the clusters behind its totals, so the page can say that a figure
came from four of six clusters.
"""

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import host_detail
from app.models.schemas import (
    ClusterMonitoringMetrics,
    MonitoringDashboardResponse,
    NodeResources,
    NodeStatus,
)
from app.services.lxd.instance import FleetInventory

GIB = 1024**3


def _offline(name):
    return NodeStatus(
        name=name,
        endpoint=f"https://{name}:8443",
        connected=False,
        reachable=False,
        error="Cannot connect to LXD endpoint.",
    )


def _online(name, *, storage_total=512 * GIB, storage_available=256 * GIB):
    return NodeStatus(
        name=name,
        endpoint=f"https://{name}:8443",
        connected=True,
        reachable=True,
        auth="trusted",
        resources=NodeResources(
            cpu_total=8,
            cpu_used=2,
            memory_total=16 * GIB,
            memory_used=4 * GIB,
            storage_total=storage_total,
            storage_used=storage_total - storage_available,
            storage_available=storage_available,
        ),
    )


class _Cache:
    def get(self, *args, **kwargs):
        return None

    def set(self, *args, **kwargs):
        return None


class _MetadataStore:
    def get_all_node_metadata(self):
        return {}


def _client(monkeypatch, nodes):
    from app.core.org_scope import org_scope_dependency
    from app.core.rbac import get_current_user
    from app.routers import monitoring as module

    class _Service:
        async def read_fleet_inventory(self):
            return FleetInventory([], [], [], {})

        def get_node_names(self):
            return [node.name for node in nodes]

        def check_node_status(self, name):
            return next(node for node in nodes if node.name == name)

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


def _dashboard(monkeypatch, nodes):
    response = _client(monkeypatch, nodes).get("/monitoring/dashboard")
    assert response.status_code == 200, response.text
    return response.json()


def _rows(body):
    return {row["node_name"]: row for row in body["nodes"]}


def test_a_fleet_with_every_cluster_offline_reports_no_percentage(monkeypatch):
    body = _dashboard(monkeypatch, [_offline("dead-a"), _offline("dead-b")])

    assert body["memory_usage_percent"] is None
    assert body["storage_usage_percent"] is None
    assert (body["clusters_reporting"], body["clusters_total"]) == (0, 2)
    for name, row in _rows(body).items():
        assert row["memory_usage_percent"] is None, name
        assert row["storage_usage_percent"] is None, name


def test_an_offline_cluster_beside_an_online_one_reports_none_for_itself(monkeypatch):
    body = _dashboard(monkeypatch, [_online("nuc2"), _offline("dead-a")])

    assert body["memory_usage_percent"] == 25.0
    assert body["storage_usage_percent"] == 50.0
    assert (body["clusters_reporting"], body["clusters_total"]) == (1, 2)
    rows = _rows(body)
    assert (rows["nuc2"]["memory_usage_percent"], rows["nuc2"]["storage_usage_percent"]) == (25.0, 50.0)
    assert (rows["dead-a"]["memory_usage_percent"], rows["dead-a"]["storage_usage_percent"]) == (None, None)


def test_a_cluster_with_no_storage_pool_reports_no_storage_percentage(monkeypatch):
    """Memory is still a reading there. Only the share with nothing under it goes."""
    body = _dashboard(monkeypatch, [_online("fresh", storage_total=0, storage_available=0)])

    assert body["memory_usage_percent"] == 25.0
    assert body["storage_usage_percent"] is None
    row = _rows(body)["fresh"]
    assert row["memory_usage_percent"] == 25.0
    assert row["storage_usage_percent"] is None


def test_a_connected_cluster_that_sent_no_resources_is_not_reporting(monkeypatch):
    """online_nodes counts it; the totals do not, and the count says which."""
    bare = NodeStatus(name="bare", endpoint="https://bare:8443", connected=True, reachable=True, auth="trusted")
    body = _dashboard(monkeypatch, [bare, _online("nuc2")])

    assert body["online_nodes"] == 2
    assert (body["clusters_reporting"], body["clusters_total"]) == (1, 2)


def test_a_body_from_an_older_release_reads_its_counts_as_unknown():
    """A cached body without the counts must not read as zero clusters reporting."""
    old = MonitoringDashboardResponse(timestamp=datetime(2026, 10, 29, tzinfo=timezone.utc))
    assert (old.clusters_reporting, old.clusters_total) == (None, None)
    assert ClusterMonitoringMetrics(node_name="x", is_online=False).memory_usage_percent is None


@pytest.mark.parametrize("tier", ["admin", "partner"])
def test_partner_and_admin_read_the_counts(monkeypatch, tier):
    body = MonitoringDashboardResponse(
        **_dashboard(monkeypatch, [_online("nuc2"), _offline("dead-a")])
    )
    seen = host_detail.monitoring_dashboard_for_tier(body, tier)
    assert (seen.clusters_reporting, seen.clusters_total) == (1, 2)


def test_a_tenant_keeps_the_counts_and_still_no_host_figure(monkeypatch):
    """The counts are cluster status, which a Tenant already reads as online and inactive."""
    body = MonitoringDashboardResponse(
        **_dashboard(monkeypatch, [_online("nuc2"), _offline("dead-a")])
    )
    seen = host_detail.monitoring_dashboard_for_tier(body, "tenant")

    assert (seen.clusters_reporting, seen.clusters_total) == (1, 2)
    assert seen.memory_usage_percent is None
    assert seen.total_memory_bytes is None
    for row in seen.nodes:
        assert row.memory_total_bytes is None
        assert row.storage_usage_percent is None
