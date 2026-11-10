"""A route that cannot read the cluster registry says so instead of answering an empty fleet.

When the registry LIST fails (Vault sealed, for example after a reboot) and nothing is known from
before, /nodes, /projects, /instances, /monitoring/dashboard, /regions and /clusters each answer one
503 body before any cache write. An empty 200 there would be cached and shown as an empty fleet. A
readable empty registry is a fresh install and still answers 200 with an empty list, and a served
last-known registry is real data.
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import vault_status
from app.core.cache import CacheManager
from app.core.error_sanitizer import public_server_detail
from app.core.registry_guard import VAULT_UNAVAILABLE, require_known_registry
from app.services.lxd.instance import FleetInventory


class _SpyCache(CacheManager):
    def __init__(self):
        super().__init__(redis_url=None)
        self.writes: list = []

    def set(self, namespace, key, value, ttl=None, *args, **kwargs):
        self.writes.append((namespace, key))
        return super().set(namespace, key, value, ttl, *args, **kwargs)


@pytest.fixture(autouse=True)
def _clean():
    vault_status.reset()
    yield
    vault_status.reset()


def _unknown() -> None:
    vault_status.mark_unreadable("VaultClientError: 503 Vault is sealed", served_last_known=False)


def _assert_vault_unavailable(raised: pytest.ExceptionInfo) -> None:
    assert raised.value.status_code == 503
    assert raised.value.detail["error"] == VAULT_UNAVAILABLE
    assert raised.value.headers["Retry-After"] == "15"
    assert raised.value.headers["Cache-Control"] == "no-store"


def test_the_guard_refuses_only_an_unknown_empty_registry() -> None:
    require_known_registry([])  # nothing tried yet
    vault_status.mark_readable()
    require_known_registry([])  # a fresh install
    vault_status.mark_unreadable("sealed", served_last_known=True)
    require_known_registry([])  # the memo was an empty registry
    _unknown()
    require_known_registry(["nuc1"])  # an org scope cannot widen an unknown registry, but a list is a list
    with pytest.raises(HTTPException) as raised:
        require_known_registry([])
    _assert_vault_unavailable(raised)


def test_the_sanitizer_keeps_the_structured_body() -> None:
    detail = {"error": VAULT_UNAVAILABLE, "message": "Cluster data cannot be read"}
    assert public_server_detail(503, detail, "ref")["error"] == VAULT_UNAVAILABLE


def _patch_cache(monkeypatch, module) -> _SpyCache:
    cache = _SpyCache()
    monkeypatch.setattr(module, "get_cache_manager", lambda: cache)
    return cache


class _Fleet:
    """An LXD service over a registry that answered `names`."""

    def __init__(self, names):
        self.names = list(names)
        self.cert_manager = SimpleNamespace(list_clusters=lambda use_cache=True: list(self.names))

    def get_node_names(self):
        return list(self.names)

    async def check_nodes_status_async(self):
        return []

    async def read_fleet_inventory(self):
        # The instance list reads the fleet through this. It names a late cluster instead of
        # raising, which these doubles never produce.
        vms, connected, incomplete = await self.list_all_vms_with_failures()
        return FleetInventory(vms, connected, incomplete, {})

    async def list_all_vms_with_failures(self):
        return [], [], []

    async def list_projects(self, host):
        raise AssertionError("no cluster to ask")


def _nodes(service, fresh=True):
    from app.routers import nodes

    return asyncio.run(nodes.get_nodes_status(lxd_service=service, fresh=fresh, _rbac=None))


def _projects(service):
    from app.routers import projects

    return asyncio.run(projects.list_all_projects(lxd_service=service, fresh=True))


def _instances(service):
    from app.routers import instances

    return asyncio.run(instances.list_instances(
        host=None, project=None, status=None, instance_type=None, offset=0, limit=0, lxd_service=service, fresh=True,
    ))


def _dashboard(service, monkeypatch):
    from app.routers import monitoring
    from app.services.monitoring_sweep import FleetRead

    async def empty_fleet(_service, _timer):
        return FleetRead(vms=[], connected=[], statuses=[])

    monkeypatch.setattr(monitoring, "read_fleet", empty_fleet)
    return asyncio.run(monitoring.get_monitoring_dashboard(lxd_service=service, fresh=True))


def _regions(service, monkeypatch):
    from app.core import vault_client
    from app.routers import regions
    from app.services import lxd_cert_manager

    monkeypatch.setattr(regions, "get_settings", lambda: SimpleNamespace(
        vault_enabled=True, vault_token="t", vault_addr="http://vault.invalid", vault_clusters_path="secret/data/clusters",
    ))
    monkeypatch.setattr(vault_client, "get_vault_client", lambda addr, token: object())
    monkeypatch.setattr(lxd_cert_manager, "LXDCertificateManager", lambda vault_client, clusters_path: service.cert_manager)
    return asyncio.run(regions.build_regions_overview(lxd_service=service, fresh=True))


def _clusters(service):
    from app.routers import cluster_registration

    return asyncio.run(cluster_registration.list_clusters(cert_manager=service.cert_manager, fresh=True))


ROUTES = {
    "nodes": ("app.routers.nodes", lambda s, mp: _nodes(s)),
    "projects": ("app.routers.projects", lambda s, mp: _projects(s)),
    "instances": ("app.routers.instances", lambda s, mp: _instances(s)),
    "monitoring": ("app.routers.monitoring", _dashboard),
    "regions": ("app.routers.regions", _regions),
    "clusters": ("app.core.cache", lambda s, mp: _clusters(s)),
}


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_an_unknown_registry_is_503_and_nothing_is_cached(monkeypatch, route) -> None:
    import importlib

    module_name, call = ROUTES[route]
    cache = _patch_cache(monkeypatch, importlib.import_module(module_name))
    _unknown()
    with pytest.raises(HTTPException) as raised:
        call(_Fleet([]), monkeypatch)
    _assert_vault_unavailable(raised)
    assert [write for write in cache.writes if write[0] == "api_responses"] == []


@pytest.mark.parametrize("route", ["nodes", "projects", "instances", "regions", "clusters"])
def test_a_readable_empty_registry_is_still_an_empty_answer(monkeypatch, route) -> None:
    import importlib

    module_name, call = ROUTES[route]
    _patch_cache(monkeypatch, importlib.import_module(module_name))
    vault_status.mark_readable()
    result = call(_Fleet([]), monkeypatch)
    rows = getattr(result, "nodes", None)
    if rows is None:
        rows = getattr(result, "vms", None)
    if rows is None:
        rows = getattr(result, "regions", None)
    if rows is None:
        rows = getattr(result, "clusters", result)
    assert list(rows) == []


def test_the_nodes_route_still_caches_a_real_empty_answer(monkeypatch) -> None:
    from app.routers import nodes

    cache = _patch_cache(monkeypatch, nodes)
    vault_status.mark_readable()
    _nodes(_Fleet([]))
    assert any(write[0] == "api_responses" for write in cache.writes)
