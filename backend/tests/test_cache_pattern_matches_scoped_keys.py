"""A cache-invalidation glob must match the keys that are written.

`get_scope_cache_key` appends a scope suffix to every cached list key: `...:scope=all` for
admin and background work, `...:scope=<digest>:acl=<digest>` for a scoped user. A cluster's
member list is stored under `orcastra:api_responses:cluster_members:list:host=<id>:scope=all`,
never the bare `...host=<id>`.

Redis KEYS treats a pattern without a wildcard as an exact match, while the memory-cache
branch of `delete_pattern` matches by prefix, so a pattern missing its `*` passes without
Redis and clears nothing in production. These tests exercise the Redis path with a fake client.
"""

import fnmatch
import unittest

from app.core.cache import (
    CacheManager,
    MEMBER_RESOURCES_CACHE_PREFIX,
    MICROCLOUD_RESOURCES_CACHE_PREFIX,
    NODES_AVAILABILITY_CACHE_KEY,
    NODES_STATUS_CACHE_KEY,
    REGIONS_OVERVIEW_CACHE_KEY,
)


class FakeRedis:
    """Enough of redis-py to exercise delete_pattern's glob semantics."""

    def __init__(self):
        self.store = {}

    def keys(self, pattern):
        # Redis KEYS is a glob: no wildcard means an exact match.
        return [k for k in self.store if fnmatch.fnmatchcase(k, pattern)]

    def scan_iter(self, *, match, count):
        assert count == 200
        yield from self.keys(match)

    def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)

    def setex(self, key, ttl, value):
        self.store[key] = value

    def get(self, key):
        return self.store.get(key)


class ScopedKeyInvalidationTests(unittest.TestCase):
    def _manager_with(self, *keys):
        manager = CacheManager(redis_url=None)
        manager.redis_client = FakeRedis()
        for key in keys:
            manager.redis_client.store[key] = "cached"
        return manager

    def test_cluster_member_invalidation_clears_the_scoped_redis_key(self):
        admin_key = "orcastra:api_responses:cluster_members:list:host=nuc2:scope=all"
        tenant_key = "orcastra:api_responses:cluster_members:list:host=nuc2:scope=ab12cd34:acl=ff00"
        manager = self._manager_with(admin_key, tenant_key)

        manager.invalidate_cluster_cache("nuc2")

        self.assertNotIn(admin_key, manager.redis_client.store)
        self.assertNotIn(tenant_key, manager.redis_client.store)

    def test_invalidation_is_scoped_to_the_named_cluster(self):
        target = "orcastra:api_responses:cluster_members:list:host=nuc2:scope=all"
        other = "orcastra:api_responses:cluster_members:list:host=nuc3:scope=all"
        manager = self._manager_with(target, other)

        manager.invalidate_cluster_cache("nuc2")

        self.assertNotIn(target, manager.redis_client.store)
        self.assertIn(other, manager.redis_client.store, "invalidating one cluster wiped another")

    def test_a_sibling_cluster_sharing_a_prefix_survives(self):
        """An id that extends another is a different cluster; a bare prefix matches both."""
        target = "orcastra:api_responses:cluster_members:list:host=nuc2:scope=all"
        sibling = "orcastra:api_responses:cluster_members:list:host=nuc22:scope=all"
        suffixed = "orcastra:api_responses:cluster_members:list:host=nuc2-prod:scope=all"
        manager = self._manager_with(target, sibling, suffixed)

        manager.invalidate_cluster_cache("nuc2")

        self.assertNotIn(target, manager.redis_client.store)
        self.assertIn(sibling, manager.redis_client.store, "invalidating nuc2 also wiped nuc22")
        self.assertIn(suffixed, manager.redis_client.store, "invalidating nuc2 also wiped nuc2-prod")

    def test_exact_match_pattern_would_not_have_matched(self):
        """Pins the mechanism: in Redis a pattern without `*` matches nothing here."""
        key = "orcastra:api_responses:cluster_members:list:host=nuc2:scope=all"
        manager = self._manager_with(key)

        manager.delete_pattern("api_responses", "cluster_members:list:host=nuc2")

        self.assertIn(key, manager.redis_client.store)

    def test_instance_invalidation_clears_every_capacity_projection(self):
        capacity_keys = {
            f"orcastra:api_responses:{NODES_STATUS_CACHE_KEY}:scope=all",
            f"orcastra:api_responses:{REGIONS_OVERVIEW_CACHE_KEY}:scope=tenant:acl=abc",
            f"orcastra:lxd_resources:{MICROCLOUD_RESOURCES_CACHE_PREFIX}:cluster-a",
            f"orcastra:lxd_resources:{MEMBER_RESOURCES_CACHE_PREFIX}:cluster-a:member-1:scope=all",
        }
        availability = f"orcastra:api_responses:{NODES_AVAILABILITY_CACHE_KEY}:scope=all"
        manager = self._manager_with(*capacity_keys, availability)

        manager.invalidate_instance_cache()

        self.assertTrue(capacity_keys.isdisjoint(manager.redis_client.store))
        self.assertIn(availability, manager.redis_client.store, "allocation changes wiped reachability")

    def test_storage_and_image_writes_clear_capacity_but_unrelated_resources_do_not(self):
        node_key = f"orcastra:api_responses:{NODES_STATUS_CACHE_KEY}:scope=all"
        manager = self._manager_with(node_key)

        manager.invalidate_resource_cache("projects")
        self.assertIn(node_key, manager.redis_client.store)

        for resource in ("images", "storage_pools", "storage_pool_resources", "storage_volumes"):
            manager.redis_client.store[node_key] = "cached"
            manager.invalidate_resource_cache(resource)
            self.assertNotIn(node_key, manager.redis_client.store, resource)


if __name__ == "__main__":
    unittest.main()
