"""Cache generation fences for monitoring and cluster-member reads.

These handlers fan out to LXD before writing relatively long-lived cache entries. A mutation
may invalidate the old entry while that fan-out is still running, so the read must not put its
pre-mutation result back after the invalidation.
"""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.cache import CacheManager
from app.routers import cluster_members, monitoring


CLUSTER = "cluster-a"
SCOPE = "org-a"


class _GenerationCache:
    def __init__(self, seeded: dict[tuple[str, str], Any] | None = None) -> None:
        self.store = dict(seeded or {})
        self.generations = {"monitoring": "monitoring-before", "cluster_members": "members-before"}
        self.writes: list[tuple[str, str, Any, int | None]] = []

    def get(self, namespace: str, key: str) -> Any:
        return self.store.get((namespace, key))

    def set(self, namespace: str, key: str, value: Any, ttl: int | None = None) -> None:
        self.store[(namespace, key)] = value
        self.writes.append((namespace, key, value, ttl))

    def resource_list_generation(self, kind: str) -> str:
        return self.generations[kind]

    def advance(self, kind: str) -> None:
        self.generations[kind] = f"{kind}-after"


def _scoped(key: str) -> str:
    return f"{key}:scope={SCOPE}"


def _api_keys(cache: _GenerationCache) -> list[str]:
    return [key for namespace, key, _value, _ttl in cache.writes if namespace == "api_responses"]


def test_instance_invalidation_advances_the_monitoring_generation() -> None:
    cache = CacheManager(redis_url=None)
    key = _scoped(monitoring.MONITORING_DASHBOARD_CACHE_KEY)
    cache.set("api_responses", key, {"stale": True})

    cache.invalidate_instance_cache()

    assert cache.resource_list_generation("monitoring") is not None
    assert cache.get("api_responses", key) is None


def test_cluster_invalidation_advances_the_member_generation() -> None:
    cache = CacheManager(redis_url=None)
    key = _scoped(f"cluster_members:list:host={CLUSTER}")
    cache.set("api_responses", key, {"stale": True})

    cache.invalidate_cluster_cache(CLUSTER)

    assert cache.resource_list_generation("cluster_members") is not None
    assert cache.get("api_responses", key) is None


class _MetadataStore:
    def get_all_node_metadata(self) -> dict[str, Any]:
        return {}


class _DashboardService:
    def __init__(self, cache: _GenerationCache, advance: bool) -> None:
        self.cache = cache
        self.advance = advance

    async def list_all_vms(self) -> tuple[list[Any], list[str]]:
        if self.advance:
            self.cache.advance("monitoring")
        return [], [CLUSTER]

    def get_node_names(self) -> list[str]:
        return []

    def check_node_status(self, name: str) -> Any:
        raise AssertionError("no cluster is registered in this fixture")


@pytest.mark.parametrize("advance", [False, True], ids=["stable", "invalidated"])
def test_dashboard_only_caches_the_generation_it_read(monkeypatch, advance: bool) -> None:
    cache = _GenerationCache()
    monkeypatch.setattr(monitoring, "get_cache_manager", lambda: cache)
    monkeypatch.setattr(monitoring, "get_scope_cache_key", _scoped)
    monkeypatch.setattr(monitoring, "get_node_metadata_store", lambda: _MetadataStore())
    monkeypatch.setattr(monitoring, "tenant_acl_active", lambda: False)
    monkeypatch.setattr(monitoring, "_last_history_store_times", {})

    result = asyncio.run(
        monitoring.get_monitoring_dashboard(
            lxd_service=_DashboardService(cache, advance),
            fresh=True,
        )
    )

    assert result.total_nodes == 0
    expected_key = _scoped(monitoring.MONITORING_DASHBOARD_CACHE_KEY)
    assert (expected_key in _api_keys(cache)) is (not advance)
    history_writes = [write for write in cache.writes if write[0] == "monitoring_history"]
    assert bool(history_writes) is (not advance)


def _node_resources() -> SimpleNamespace:
    return SimpleNamespace(
        cpu_total=8,
        cpu_used=2,
        memory_total=16 * 1024**3,
        memory_used=4 * 1024**3,
        storage_total=1000,
        storage_available=600,
        gpu_total=1,
        gpu_used=0,
        gpu_available=1,
        gpu_cards_total=1,
        gpu_integrated_total=0,
        gpu_models=["Example GPU"],
        gpu_utilization_supported=False,
    )


class _NodeService:
    def __init__(self, cache: _GenerationCache, advance: bool) -> None:
        self.cache = cache
        self.advance = advance

    async def check_nodes_status_async(self) -> list[Any]:
        if self.advance:
            self.cache.advance("monitoring")
        return [
            SimpleNamespace(
                name=CLUSTER,
                connected=True,
                node_type="lxd",
                resources=_node_resources(),
            )
        ]

    async def list_all_vms(self) -> tuple[list[Any], list[str]]:
        return [], [CLUSTER]


@pytest.mark.parametrize("advance", [False, True], ids=["stable", "invalidated"])
def test_node_metrics_only_cache_the_generation_they_read(monkeypatch, advance: bool) -> None:
    cache = _GenerationCache()
    monkeypatch.setattr(monitoring, "get_cache_manager", lambda: cache)
    monkeypatch.setattr(monitoring, "get_scope_cache_key", _scoped)
    monkeypatch.setattr(monitoring, "verify_cluster_access", lambda _node: None)

    result = asyncio.run(
        monitoring.get_node_metrics(
            CLUSTER,
            lxd_service=_NodeService(cache, advance),
            fresh=True,
        )
    )

    assert result.node_name == CLUSTER
    expected_key = _scoped(f"monitoring:node:{CLUSTER}")
    assert (expected_key in _api_keys(cache)) is (not advance)


class _MembersResponse:
    def __init__(self, cache: _GenerationCache, advance: bool) -> None:
        self.cache = cache
        self.advance = advance

    def json(self) -> dict[str, Any]:
        if self.advance:
            self.cache.advance("cluster_members")
        return {
            "metadata": [
                {
                    "server_name": "member-a",
                    "url": "https://member-a:8443",
                    "status": "Online",
                }
            ]
        }


class _MembersEndpoint:
    def __init__(self, cache: _GenerationCache, advance: bool) -> None:
        self.cache = cache
        self.advance = advance

    def get(self, **_kwargs: Any) -> _MembersResponse:
        return _MembersResponse(self.cache, self.advance)


def _members_client(cache: _GenerationCache, advance: bool) -> SimpleNamespace:
    return SimpleNamespace(
        host_info={"environment": {"server_clustered": True}},
        api=SimpleNamespace(
            cluster=SimpleNamespace(members=_MembersEndpoint(cache, advance))
        ),
    )


@pytest.mark.parametrize("advance", [False, True], ids=["stable", "invalidated"])
def test_member_list_only_caches_the_generation_it_read(monkeypatch, advance: bool) -> None:
    cache = _GenerationCache()

    async def client_for(_service: Any, _node: str) -> Any:
        return _members_client(cache, advance)

    monkeypatch.setattr(cluster_members, "get_cache_manager", lambda: cache)
    monkeypatch.setattr(cluster_members, "get_scope_cache_key", _scoped)
    monkeypatch.setattr(cluster_members, "verify_cluster_access", lambda _node: None)
    monkeypatch.setattr(cluster_members, "client_for", client_for)

    result = asyncio.run(
        cluster_members.list_cluster_members(
            node=CLUSTER,
            lxd_service=object(),
            fresh=True,
        )
    )

    assert result.total == 1
    expected_key = _scoped(f"cluster_members:list:host={CLUSTER}")
    assert (expected_key in _api_keys(cache)) is (not advance)


def test_member_list_cache_hit_is_exactly_scope_bound(monkeypatch) -> None:
    key = _scoped(f"cluster_members:list:host={CLUSTER}")
    cached = cluster_members.ClusterMembersResponse(
        node=CLUSTER,
        is_clustered=True,
        cluster_enabled=True,
        members=[],
        total=0,
    ).model_dump()
    cache = _GenerationCache({("api_responses", key): cached})

    async def fail_client_for(_service: Any, _node: str) -> Any:
        raise AssertionError("a scoped cache hit must not contact the cluster")

    monkeypatch.setattr(cluster_members, "get_cache_manager", lambda: cache)
    monkeypatch.setattr(cluster_members, "get_scope_cache_key", _scoped)
    monkeypatch.setattr(cluster_members, "verify_cluster_access", lambda _node: None)
    monkeypatch.setattr(cluster_members, "client_for", fail_client_for)

    result = asyncio.run(
        cluster_members.list_cluster_members(
            node=CLUSTER,
            lxd_service=object(),
            fresh=False,
        )
    )

    assert result.node == CLUSTER
    assert cache.writes == []
