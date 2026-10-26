"""A Tenant reads Regions as places and its own instances, never as fleet hardware.

Hardware inventory is Partner and above: GET /nodes requires cluster:read, and the
availability projection strips the hypervisor type from a Tenant's rows. GET /regions
answered every role with each cluster's hardware threads, memory, storage, GPU models and
hypervisor type, plus fleet totals, so a Tenant read through Regions exactly what
Technologies keeps from it.

The body is cached per org scope and ACL, and a Tenant whose ACL is not enforced shares the
key of a Partner with the same clusters. The projection therefore runs on every answer,
cached or fresh, and builds new models rather than editing the cached one.
"""

import asyncio
import json
from datetime import datetime, timezone

from app.core.auth import AuthenticatedUser
from app.models.schemas import (
    ExtendedHardwareInfo,
    RegionNodeInfo,
    RegionSummary,
    RegionsOverviewResponse,
)
from app.routers import regions
from app.services import metrics_prewarm

TENANT = AuthenticatedUser(user_id="t", email="t@example.com", groups=["role_tenant"])
PARTNER = AuthenticatedUser(user_id="p", email="p@example.com", groups=["role_partner"])
ADMIN = AuthenticatedUser(user_id="a", email="a@example.com", groups=["role_admin"])
NO_ROLE = AuthenticatedUser(user_id="n", email="n@example.com", groups=[])

HARDWARE = ExtendedHardwareInfo(
    cpu_total=64,
    cpu_sockets=2,
    cpu_vendor="Intel",
    cpu_model="Xeon Gold 6338",
    memory_total=512 * 1024**3,
    memory_used=100 * 1024**3,
    gpu_total=2,
    gpu_cards_total=3,
    gpu_vendors=["NVIDIA"],
    gpu_models=["A100"],
    storage_total=10 * 1024**4,
    storage_used=1024**4,
    storage_types=["NVMe"],
    storage_drivers=["zfs"],
    network_interfaces=["eno1"],
)


def _overview() -> RegionsOverviewResponse:
    node = RegionNodeInfo(
        cluster_id="nuc2",
        display_name="NUC 2",
        status="online",
        node_type="microcloud",
        hardware=HARDWARE,
    )
    region = RegionSummary(
        region_id="id-jkt",
        region_name="Jakarta",
        country_code="ID",
        country_name="Indonesia",
        city="Jakarta",
        latitude=-6.2,
        longitude=106.8,
        continent="AS",
        facility_name="DC One",
        node_count=1,
        instance_count=3,
        instance_running=2,
        hardware=HARDWARE,
        nodes=[node],
    )
    return RegionsOverviewResponse(
        regions=[region],
        total_nodes=1,
        total_instances=3,
        total_cpu=64,
        total_memory=HARDWARE.memory_total,
        total_storage=HARDWARE.storage_total,
        total_gpu=2,
        total_gpu_cards=3,
        total_gpu_integrated=0,
        collected_at=datetime(2026, 10, 26, 7, 0, tzinfo=timezone.utc),
    )


def _serve(monkeypatch, body: RegionsOverviewResponse):
    async def build(lxd_service, fresh=False):
        return body

    monkeypatch.setattr(regions, "build_regions_overview", build)


def test_a_tenant_overview_carries_no_hardware_type_or_totals(monkeypatch):
    _serve(monkeypatch, _overview())
    seen = asyncio.run(regions.get_regions_overview(lxd_service=None, fresh=False, user=TENANT))

    for total in ("total_cpu", "total_memory", "total_storage", "total_gpu",
                  "total_gpu_cards", "total_gpu_integrated"):
        assert getattr(seen, total) is None, f"{total} reached a Tenant"
    region = seen.regions[0]
    assert region.hardware is None
    assert region.nodes[0].hardware is None
    assert region.nodes[0].node_type is None

    wire = json.dumps(seen.model_dump(), default=str)
    for leaked in ("Xeon", "A100", "NVIDIA", "microcloud", "eno1", "zfs", "NVMe"):
        assert leaked not in wire, f"{leaked!r} reached a Tenant"


def test_a_tenant_keeps_places_clusters_and_its_own_instance_counts(monkeypatch):
    _serve(monkeypatch, _overview())
    seen = asyncio.run(regions.get_regions_overview(lxd_service=None, fresh=False, user=TENANT))

    assert seen.total_nodes == 1
    assert seen.total_instances == 3
    assert seen.collected_at == datetime(2026, 10, 26, 7, 0, tzinfo=timezone.utc)
    region = seen.regions[0]
    assert (region.region_id, region.city, region.facility_name) == ("id-jkt", "Jakarta", "DC One")
    assert (region.latitude, region.longitude) == (-6.2, 106.8)
    assert (region.node_count, region.instance_count, region.instance_running) == (1, 3, 2)
    node = region.nodes[0]
    assert (node.cluster_id, node.display_name, node.status) == ("nuc2", "NUC 2", "online")


def test_partner_and_admin_read_the_body_unchanged(monkeypatch):
    for user in (PARTNER, ADMIN):
        body = _overview()
        _serve(monkeypatch, body)
        seen = asyncio.run(regions.get_regions_overview(lxd_service=None, fresh=False, user=user))
        assert seen.model_dump() == _overview().model_dump()


def test_a_caller_without_a_mapped_role_is_treated_as_a_tenant(monkeypatch):
    # note: the session layer refuses such a caller first; this is the second line.
    _serve(monkeypatch, _overview())
    seen = asyncio.run(regions.get_regions_overview(lxd_service=None, fresh=False, user=NO_ROLE))
    assert seen.total_cpu is None
    assert seen.regions[0].hardware is None


def test_the_projection_leaves_the_cached_body_intact_for_the_next_caller(monkeypatch):
    shared = _overview()
    before = shared.model_dump()
    _serve(monkeypatch, shared)

    asyncio.run(regions.get_regions_overview(lxd_service=None, fresh=False, user=TENANT))
    assert shared.model_dump() == before, "the Tenant projection edited the shared body"

    partner_view = asyncio.run(
        regions.get_regions_overview(lxd_service=None, fresh=False, user=PARTNER)
    )
    assert partner_view.regions[0].nodes[0].hardware.cpu_model == "Xeon Gold 6338"


def test_region_details_are_projected_the_same_way(monkeypatch):
    _serve(monkeypatch, _overview())

    tenant_region = asyncio.run(
        regions.get_region_details("id-jkt", lxd_service=None, user=TENANT)
    )
    assert tenant_region.hardware is None
    assert tenant_region.nodes[0].hardware is None
    assert tenant_region.nodes[0].node_type is None
    assert tenant_region.instance_count == 3

    partner_region = asyncio.run(
        regions.get_region_details("id-jkt", lxd_service=None, user=PARTNER)
    )
    assert partner_region.hardware.cpu_total == 64
    assert partner_region.nodes[0].node_type == "microcloud"


def test_the_cache_prewarm_builds_the_unprojected_body(monkeypatch):
    # The prewarm has no caller and writes the shared entry, so it must use the builder: the
    # route needs a caller and would fail quietly here, leaving the entry cold.
    _serve(monkeypatch, _overview())
    warm = metrics_prewarm.CachePrewarm()
    warm._lxd_service = None
    body = asyncio.run(warm._fetch_regions())
    assert body["total_cpu"] == 64
    assert body["regions"][0]["nodes"][0]["hardware"]["cpu_model"] == "Xeon Gold 6338"
