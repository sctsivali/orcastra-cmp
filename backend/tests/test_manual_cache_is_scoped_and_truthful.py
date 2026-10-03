"""Private cache refreshes preserve other scopes and fail honestly."""

import json

import pytest

from app.core.cache import (
    CACHE_CLEAR_GENERATION_KEY, CAPACITY_GENERATION_KIND, CacheInvalidationError,
    CacheManager, MEMBER_RESOURCES_CACHE_PREFIX, MICROCLOUD_RESOURCES_CACHE_PREFIX,
    NODES_AVAILABILITY_CACHE_KEY, NODES_STATUS_CACHE_KEY, REGIONS_OVERVIEW_CACHE_KEY,
)
from app.core.cache_glob import compile_cache_glob


class ScanRedis:
    def __init__(self):
        self.values = {}
        self.batches = []
        self.fail = False

    def get(self, key):
        return self.values.get(key)

    def setex(self, key, _ttl, value):
        self.values[key] = value

    def scan_iter(self, *, match, count):
        assert count == 200
        matcher = compile_cache_glob(match)
        yield from [key for key in self.values if matcher.fullmatch(key)]

    def delete(self, *keys):
        self.batches.append(len(keys))
        if self.fail:
            raise RuntimeError("synthetic Redis failure")
        for key in keys:
            self.values.pop(key, None)


def manager(shared):
    cache = CacheManager(redis_url=None)
    if shared:
        cache.redis_client = ScanRedis()
    return cache


@pytest.mark.parametrize("shared", [False, True])
def test_namespace_clear_never_clears_a_neighbour(shared):
    cache = manager(shared)
    cache.set("api_responses", "x", {"stale": True})
    cache.set("tags", "x", {"keep": True})
    cache.delete_pattern("api_responses")
    assert cache.get("api_responses", "x") is None
    assert cache.get("tags", "x") == {"keep": True}


@pytest.mark.parametrize("shared", [False, True])
def test_exact_and_redis_glob_matching_are_identical(shared):
    cache = manager(shared)
    for key in ("node", "node2", "nodea", "nodeb", "node*", "other"):
        cache.set("api_responses", key, {"value": True})
    cache.delete_pattern("api_responses", "node")
    assert cache.get("api_responses", "node2") is not None
    cache.delete_pattern("api_responses", "node[^2a]")
    assert cache.get("api_responses", "nodeb") is None
    assert cache.get("api_responses", "nodea") is not None
    cache.delete_pattern("api_responses", r"node\*")
    assert cache.get("api_responses", "node2") is not None


@pytest.mark.parametrize("shared", [False, True])
def test_cluster_refresh_preserves_other_cluster_and_actor_aggregates(shared):
    cache = manager(shared)
    aggregates = (NODES_STATUS_CACHE_KEY, NODES_AVAILABILITY_CACHE_KEY, REGIONS_OVERVIEW_CACHE_KEY)
    own = tuple(f"{key}:scope=actor-a" for key in aggregates)
    other = tuple(f"{key}:scope=actor-b" for key in aggregates)
    target = (
        f"{MICROCLOUD_RESOURCES_CACHE_PREFIX}:cluster-a",
        f"{MEMBER_RESOURCES_CACHE_PREFIX}:cluster-a:member:scope=actor-a",
        "cluster_members:list:host=cluster-a:scope=actor-b",
    )
    keep = (
        f"{MICROCLOUD_RESOURCES_CACHE_PREFIX}:cluster-ab",
        f"{MEMBER_RESOURCES_CACHE_PREFIX}:cluster-ab:member:scope=actor-a",
        "cluster_members:list:host=cluster-ab:scope=actor-b",
    )
    for key in own + other:
        cache.set("api_responses", key, {"cached": True})
    for key in target + keep:
        cache.set("api_responses" if key.startswith("cluster_members") else "lxd_resources", key, {"cached": True})
    before = cache.resource_list_generation(CAPACITY_GENERATION_KIND)
    cache.invalidate_node_cache("cluster-a", own)
    assert cache.resource_list_generation(CAPACITY_GENERATION_KIND) != before
    for key in own:
        assert cache.get("api_responses", key) is None
    for key in other:
        assert cache.get("api_responses", key) is not None
    for keys, present in ((target, False), (keep, True)):
        for key in keys:
            result = cache.get("api_responses" if key.startswith("cluster_members") else "lxd_resources", key)
            assert (result is not None) == present


def test_scan_deletes_in_bounded_batches():
    cache = manager(True)
    for index in range(507):
        cache.redis_client.values[f"orcastra:tags:item-{index}"] = json.dumps(index)
    cache.delete_pattern("tags")
    assert cache.redis_client.batches == [200, 200, 107]


@pytest.mark.parametrize("shared", [False, True])
def test_global_clear_preserves_new_generation_against_an_inflight_read(shared):
    cache = manager(shared)
    before = (cache.instance_list_generation(), cache.resource_list_generation("profiles"), cache.snapshot_fleet_generation())
    cache.set("api_responses", "profiles:list:scope=all", {"cached": True})
    cleared, failed = cache.clear_namespaces(("api_responses", "tags"))
    after = (cache.instance_list_generation(), cache.resource_list_generation("profiles"), cache.snapshot_fleet_generation())
    assert cleared == ["api_responses", "tags"] and failed == []
    assert all(old != new for old, new in zip(before, after))
    assert cache.get("api_responses", CACHE_CLEAR_GENERATION_KEY) is not None
    assert cache.get("api_responses", "profiles:list:scope=all") is None


def test_partial_shared_clear_reports_failure_and_still_clears_local_copy():
    cache = manager(True)
    cache.set("tags", "x", {"cached": True})
    cache.redis_client.fail = True
    cleared, failed = cache.clear_namespaces(("tags",))
    assert cleared == [] and failed == ["tags"]
    assert cache._make_key("tags", "x") not in cache.memory_cache.cache
    with pytest.raises(CacheInvalidationError):
        cache.invalidate_node_cache("cluster-a", ())


@pytest.mark.parametrize("shared", [False, True])
def test_admin_fleet_refresh_clears_projections_and_preserves_registry(shared):
    cache = manager(shared)
    for namespace, key in (
        ("api_responses", f"{NODES_STATUS_CACHE_KEY}:scope=actor-a"),
        ("api_responses", f"{NODES_AVAILABILITY_CACHE_KEY}:scope=actor-b"),
        ("api_responses", "cluster_members:list:host=cluster-a:scope=actor-a"),
        ("lxd_resources", f"{MICROCLOUD_RESOURCES_CACHE_PREFIX}:cluster-a"),
        ("vault_metadata", "registry"),
    ):
        cache.set(namespace, key, {"cached": True})
    before = cache.resource_list_generation(CAPACITY_GENERATION_KIND)
    cache.invalidate_all_node_caches()
    assert cache.resource_list_generation(CAPACITY_GENERATION_KIND) != before
    assert cache.get("api_responses", f"{NODES_STATUS_CACHE_KEY}:scope=actor-a") is None
    assert cache.get("api_responses", f"{NODES_AVAILABILITY_CACHE_KEY}:scope=actor-b") is None
    assert cache.get("api_responses", "cluster_members:list:host=cluster-a:scope=actor-a") is None
    assert cache.get("lxd_resources", f"{MICROCLOUD_RESOURCES_CACHE_PREFIX}:cluster-a") is None
    assert cache.get("vault_metadata", "registry") is not None


def test_failed_admin_fleet_refresh_clears_every_local_projection_then_reports_failure():
    cache = manager(True)
    target_keys = (
        f"{NODES_STATUS_CACHE_KEY}:scope=actor-a",
        f"{NODES_AVAILABILITY_CACHE_KEY}:scope=actor-b",
        "cluster_members:list:host=cluster-a:scope=actor-a",
    )
    for key in target_keys:
        cache.set("api_responses", key, {"cached": True})
    cache.redis_client.fail = True
    with pytest.raises(CacheInvalidationError):
        cache.invalidate_all_node_caches()
    for key in target_keys:
        assert cache._make_key("api_responses", key) not in cache.memory_cache.cache
