"""The regions overview answers from its cache, stale included, and never waits on one slow host.

A plain cache with the same lifetime as the page's poll interval would expire just before each
poll and pay the whole fan-out. The overview goes through swr.serve on one module pool with a wall
per cluster: a cluster still reading past the wall is left out of that answer, which is remembered
for LATE_FRESH_SECONDS only, so the next poll asks it again soon.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from app.core import swr, vault_client
from app.routers import regions
from app.services import lxd_cert_manager
from app.services.lxd.fanout import LATE_FRESH_SECONDS


def _setup(monkeypatch, *, delay: float = 0.0):
    reads = []
    location = {
        "region_id": "id-jkt", "region_name": "Jakarta", "country_code": "ID",
        "country_name": "Indonesia", "city": "Jakarta", "latitude": -6.2, "longitude": 106.8,
        "continent": "AS",
    }
    monkeypatch.setattr(regions, "get_settings", lambda: SimpleNamespace(
        vault_enabled=True, vault_token="t", vault_addr="http://vault.invalid",
        vault_clusters_path="secret/data/clusters",
    ))
    cert_manager = SimpleNamespace(
        list_clusters=lambda use_cache=True: ["nuc2"],
        get_cluster_info=lambda cluster_id: {"metadata": {"display_name": "NUC 2", "location": location}},
        get_cluster_certificate=lambda cluster_id: (_ for _ in ()).throw(RuntimeError("no certificate")),
    )
    monkeypatch.setattr(vault_client, "get_vault_client", lambda addr, token: object())
    monkeypatch.setattr(lxd_cert_manager, "LXDCertificateManager", lambda vault_client, clusters_path: cert_manager)
    client = SimpleNamespace(storage_pools=SimpleNamespace(all=lambda: []))
    service = SimpleNamespace(connection_manager=SimpleNamespace(get_client=lambda cluster_id: client))
    monkeypatch.setattr(regions, "resolve_cluster_type", lambda stored, live: "lxd")
    monkeypatch.setattr(regions, "resolve_clustered", lambda stored, live: False)

    def read_resources(client, cluster_id):
        reads.append(cluster_id)
        time.sleep(delay)
        return SimpleNamespace(body={})

    monkeypatch.setattr(regions, "read_resources", read_resources)
    monkeypatch.setattr(regions, "list_instance_records", lambda client: ([], None))
    return service, reads


def _spy_fresh_for(monkeypatch):
    seen = []
    real = swr._build_and_store

    async def spy(cache, key, base_key, build, generation, label):
        built = await real(cache, key, base_key, build, generation, label)
        seen.append(built.fresh_for)
        return built

    monkeypatch.setattr(swr, "_build_and_store", spy)
    return seen


def test_a_second_poll_reads_no_cluster(monkeypatch):
    service, reads = _setup(monkeypatch)
    first = asyncio.run(regions.build_regions_overview(lxd_service=service, fresh=False))
    second = asyncio.run(regions.build_regions_overview(lxd_service=service, fresh=False))
    assert reads == ["nuc2"]
    assert first.total_nodes == second.total_nodes == 1


def test_fresh_reads_the_clusters_again(monkeypatch):
    service, reads = _setup(monkeypatch)
    asyncio.run(regions.build_regions_overview(lxd_service=service, fresh=False))
    asyncio.run(regions.build_regions_overview(lxd_service=service, fresh=True))
    assert reads == ["nuc2", "nuc2"]


def test_a_cluster_past_its_wall_is_left_out_and_asked_again_soon(monkeypatch):
    service, reads = _setup(monkeypatch, delay=0.6)
    monkeypatch.setattr(regions, "_CLUSTER_WALL_SECONDS", 0.2)
    seen = _spy_fresh_for(monkeypatch)
    started = time.monotonic()
    body = asyncio.run(regions.build_regions_overview(lxd_service=service, fresh=True))
    assert time.monotonic() - started < 0.55, "the overview waited for the slow cluster"
    assert body.total_nodes == 0
    assert seen == [LATE_FRESH_SECONDS]


def test_a_complete_overview_is_remembered_for_its_full_ttl(monkeypatch):
    service, _ = _setup(monkeypatch)
    seen = _spy_fresh_for(monkeypatch)
    asyncio.run(regions.build_regions_overview(lxd_service=service, fresh=True))
    assert seen == [regions._FRESH_SECONDS]
