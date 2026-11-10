"""A Tenant monitors its own instances and the clusters' status, never the hosts under them.

Host figures (hardware threads, memory, storage, GPU models, hypervisor type, the raw status
error, fleet totals and each host's load) describe every other party's instances too. GET
/monitoring/dashboard strips them for a Tenant, and GET /monitoring/history (host load over time)
and GET /monitoring/nodes/{node}/metrics (one cluster's hardware) refuse a Tenant.
"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import host_detail
from app.core.auth import AuthenticatedUser
from app.core.org_scope import org_scope_dependency
from app.core.rbac import get_current_user
from app.models.schemas import (
    ClusterMonitoringMetrics,
    InstanceMonitoringSummary,
    MonitoringDashboardResponse,
    StoragePoolMetrics,
    WarningsSummary,
)
from app.routers import monitoring

HIDDEN_TOTALS = (
    "total_cpu", "used_cpu", "cpu_usage_percent", "realtime_cpu_percent", "cpu_busy_threads",
    "cpu_busy_percent", "cpu_reporting_threads", "total_memory_bytes", "used_memory_bytes",
    "memory_usage_percent", "realtime_memory_bytes", "realtime_memory_percent",
    "total_storage_bytes", "used_storage_bytes", "storage_usage_percent", "total_gpu",
    "total_gpu_cards", "total_gpu_display_cards", "total_gpu_integrated_cards", "used_gpu",
)
# cpu_reporting_instances counts the Tenant's own running instances that reported CPU.
KEPT_NODE_FIELDS = (
    "node_name", "is_online", "cluster_status", "collection_timed_out", "cpu_reporting_instances",
    "instance_count", "running_instances", "container_count", "vm_count",
)


def _dashboard() -> MonitoringDashboardResponse:
    node = ClusterMonitoringMetrics(
        node_name="nuc2", node_type="microcloud", is_online=True, cluster_status="online",
        status_message="Could not reach https://10.9.8.49:9443 on member nuc2-b",
        cpu_total=64, cpu_used=80, cpu_usage_percent=125.0, cpu_allocated=80,
        cpu_allocation_ratio=1.25, cpu_overcommitted=True, cpu_busy_threads=12.5,
        cpu_busy_percent=19.5, cpu_status="ready", cpu_reporting_instances=4,
        memory_total_bytes=512 * 1024**3, memory_used_bytes=300 * 1024**3, memory_usage_percent=58.6,
        storage_total_bytes=10 * 1024**4, storage_used_bytes=4 * 1024**4, storage_usage_percent=40.0,
        gpu_total=2, gpu_used=1, gpu_available=1, gpu_cards_total=3, gpu_integrated_total=0, gpu_display_total=1,
        gpu_models=["A100"], gpu_utilization_supported=True,
        instance_count=5, running_instances=3, container_count=4, vm_count=1,
    )
    return MonitoringDashboardResponse(
        timestamp=datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc),
        total_nodes=2, online_nodes=1, inactive_nodes=1,
        total_instances=6, running_instances=3, stopped_instances=3, other_instances=0,
        container_count=5, vm_count=1,
        total_cpu=64, used_cpu=80, cpu_usage_percent=125.0, realtime_cpu_percent=19.5,
        cpu_busy_threads=12.5, cpu_busy_percent=19.5, cpu_status="ready",
        cpu_reporting_instances=4, cpu_reporting_threads=64,
        total_memory_bytes=512 * 1024**3, used_memory_bytes=300 * 1024**3, memory_usage_percent=58.6,
        realtime_memory_bytes=20 * 1024**3, realtime_memory_percent=3.9,
        total_storage_bytes=10 * 1024**4, used_storage_bytes=4 * 1024**4, storage_usage_percent=40.0,
        total_gpu=2, total_gpu_cards=3, total_gpu_display_cards=1, total_gpu_integrated_cards=0, used_gpu=1,
        nodes=[node],
        storage_pools=[StoragePoolMetrics(
            name="local", node="nuc2", driver="zfs", total_bytes=1, used_bytes=1,
            available_bytes=0, usage_percent=100.0,
        )],
        consumers=[InstanceMonitoringSummary(
            name="web", host="nuc2", status="Running", memory_usage_bytes=1024,
        )],
        warnings=WarningsSummary(total=5),
        collection_time_ms=1200.0,
    )


def test_a_tenant_dashboard_carries_no_host_figure():
    seen = host_detail.monitoring_dashboard_for_tier(_dashboard(), "tenant")

    for field in HIDDEN_TOTALS:
        assert getattr(seen, field) is None, f"{field} reached a tenant"
    # The fleet list is a deny-list, so a GPU total added later must be added there too.
    for field in type(seen).model_fields:
        if "gpu" in field:
            assert getattr(seen, field) is None, f"{field} reached a tenant; add it to FLEET_HOST_FIGURES"
    assert seen.storage_pools == []
    assert seen.warnings is None
    node = seen.nodes[0]
    for field, value in node.model_dump().items():
        if field in KEPT_NODE_FIELDS:
            continue
        assert value in (None, [], "unavailable"), f"node {field}={value!r} reached a tenant"
    wire = seen.model_dump_json()
    for leaked in ("microcloud", "A100", "10.9.8.49", "nuc2-b", "zfs"):
        assert leaked not in wire, f"{leaked!r} reached a tenant"


def test_a_tenant_keeps_cluster_status_and_its_own_instances():
    seen = host_detail.monitoring_dashboard_for_tier(_dashboard(), "tenant")
    assert (seen.total_nodes, seen.online_nodes, seen.inactive_nodes) == (2, 1, 1)
    assert (seen.total_instances, seen.running_instances, seen.stopped_instances) == (6, 3, 3)
    assert (seen.container_count, seen.vm_count) == (5, 1)
    assert [c.name for c in seen.consumers] == ["web"]
    node = seen.nodes[0]
    assert {f: getattr(node, f) for f in KEPT_NODE_FIELDS} == {
        "node_name": "nuc2", "is_online": True, "cluster_status": "online",
        "collection_timed_out": False, "cpu_reporting_instances": 4, "instance_count": 5,
        "running_instances": 3, "container_count": 4, "vm_count": 1,
    }


@pytest.mark.parametrize("tier", ["admin", "partner"])
def test_partner_and_admin_read_the_dashboard_unchanged(tier):
    assert host_detail.monitoring_dashboard_for_tier(_dashboard(), tier).model_dump() == _dashboard().model_dump()


def test_the_projection_leaves_the_served_body_intact():
    body = _dashboard()
    before = body.model_dump()
    host_detail.monitoring_dashboard_for_tier(body, "tenant")
    assert body.model_dump() == before


def test_the_dashboard_route_projects_what_it_serves(monkeypatch):
    async def serve(**_kwargs):
        return SimpleNamespace(as_model=lambda model: _dashboard())

    monkeypatch.setattr(monitoring.swr, "serve", serve)
    monkeypatch.setattr(monitoring, "caller_tier", lambda: "tenant")
    seen = asyncio.run(monitoring.get_monitoring_dashboard(lxd_service=None, fresh=False))
    assert seen.total_cpu is None and seen.nodes[0].cpu_total is None

    monkeypatch.setattr(monitoring, "caller_tier", lambda: "partner")
    seen = asyncio.run(monitoring.get_monitoring_dashboard(lxd_service=None, fresh=False))
    assert seen.total_cpu == 64 and seen.nodes[0].node_type == "microcloud"


def _client_as(groups):
    app = FastAPI()
    app.include_router(monitoring.router)
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        user_id="u", email="u@example.com", groups=groups
    )
    app.dependency_overrides[org_scope_dependency] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("path", ["/monitoring/history?window=300", "/monitoring/nodes/nuc2/metrics"])
def test_host_load_and_one_cluster_hardware_are_refused_to_a_tenant(path):
    response = _client_as(["role_tenant"]).get(path)
    assert response.status_code == 403, response.text[:200]
