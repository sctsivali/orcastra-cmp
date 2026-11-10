"""The instance lists can be cleared alone, and the full instance clearance still clears it all.

`invalidate_instance_lists` is the fast tier the event-stream clearance runs every 10 s at most:
it advances `instance_list_generation`, the token /instances and /vms fence on, and clears only
`instances:*` and `vms:*`. `invalidate_instance_cache` runs it first and then every `used_by`
family, IPAM and capacity. `invalidate_project_caches` is the family a project write clears.
"""

from app.core.cache import (
    CAPACITY_GENERATION_KIND,
    NODES_STATUS_CACHE_KEY,
    CacheManager,
)

DEPENDENT = (
    "monitoring", "project_usage", "profiles", "networks", "storage_pools",
    "storage_pool_resources", "storage_volumes", "snapshots_fleet",
)


def _seed(manager: CacheManager) -> None:
    for key in ("instances:list:scope=all", "vms:list:scope=all", "ipam:list:v1:host=a:network=b",
                f"{NODES_STATUS_CACHE_KEY}:scope=all", *(f"{kind}:list:scope=all" for kind in DEPENDENT)):
        manager.set("api_responses", key, {"stale": True})


def _cached(manager: CacheManager, key: str) -> bool:
    return manager.get("api_responses", key) is not None


def test_the_lists_alone_advance_the_generation_and_keep_every_dependent_family():
    manager = CacheManager(redis_url=None)
    _seed(manager)
    before = manager.instance_list_generation()

    manager.invalidate_instance_lists()

    assert manager.instance_list_generation() not in (None, before), "a read in flight must see a new token"
    assert not _cached(manager, "instances:list:scope=all")
    assert not _cached(manager, "vms:list:scope=all")
    for kind in DEPENDENT:
        assert _cached(manager, f"{kind}:list:scope=all"), kind
        assert manager.resource_list_generation(kind) is None, kind
    assert _cached(manager, "ipam:list:v1:host=a:network=b")
    assert _cached(manager, f"{NODES_STATUS_CACHE_KEY}:scope=all")


def test_the_full_instance_clearance_still_clears_everything_it_did():
    manager = CacheManager(redis_url=None)
    _seed(manager)
    before = manager.instance_list_generation()

    manager.invalidate_instance_cache()

    assert manager.instance_list_generation() not in (None, before)
    assert not _cached(manager, "instances:list:scope=all")
    assert not _cached(manager, "vms:list:scope=all")
    for kind in DEPENDENT:
        assert not _cached(manager, f"{kind}:list:scope=all"), kind
        assert manager.resource_list_generation(kind) is not None, kind
    assert manager.snapshot_fleet_generation() is not None
    assert not _cached(manager, "ipam:list:v1:host=a:network=b")
    assert not _cached(manager, f"{NODES_STATUS_CACHE_KEY}:scope=all")
    assert manager.resource_list_generation(CAPACITY_GENERATION_KIND) is not None


def test_the_full_clearance_advances_the_list_generation_before_it_sweeps():
    events = []

    class Ordered(CacheManager):
        def set(self, namespace, key, value, ttl=None, **kw):
            if key == "instance_list_generation":
                events.append("generation")
            return super().set(namespace, key, value, ttl, **kw)

        def delete_pattern(self, namespace, pattern="*", **kw):
            events.append(pattern)
            return super().delete_pattern(namespace, pattern, **kw)

    Ordered(redis_url=None).invalidate_instance_cache()

    assert events[:3] == ["generation", "instances:*", "vms:*"]


def test_a_project_write_clears_the_project_family():
    manager = CacheManager(redis_url=None)
    family = ("projects", "project_usage", "profiles", "networks", "images", "storage_volumes")
    for kind in (*family, "instances", "storage_pools"):
        manager.set("api_responses", f"{kind}:list:scope=all", {"stale": True})

    manager.invalidate_project_caches()

    for kind in family:
        assert not _cached(manager, f"{kind}:list:scope=all"), kind
        assert manager.resource_list_generation(kind) is not None, kind
    assert _cached(manager, "instances:list:scope=all")
    assert _cached(manager, "storage_pools:list:scope=all")
