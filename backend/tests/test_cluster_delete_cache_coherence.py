"""Cluster deregistration clears every modern list shape and fences racing reads."""

import inspect
import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app.core.cache import CacheManager  # noqa: E402
from app.routers import cluster_registration  # noqa: E402


def test_cluster_delete_uses_broad_invalidators_and_keeps_targeted_cleanup() -> None:
    source = inspect.getsource(cluster_registration.delete_cluster)

    assert "cache.invalidate_cluster_cache(cluster_id)" in source
    assert "cache.invalidate_instance_cache()" in source
    assert 'cache.invalidate_resource_cache("clusters", "projects", "regions")' in source

    # These pre-v2 exact keys matched neither pagination/project dimensions nor every scope.
    for obsolete in (
        "instances:list:host=None:status=None:type=None",
        "vms:list:host=None:status=None",
        "projects:list:all",
    ):
        assert obsolete not in source

    # Node pickers and status have independent caches and must still be refreshed.
    assert "NODES_AVAILABILITY_CACHE_KEY" in source
    assert "NODES_STATUS_CACHE_KEY" in source


def test_the_delete_invalidators_cover_v2_scopes_and_advance_generation() -> None:
    cache = CacheManager(redis_url=None)
    cached_keys = (
        "instances:list:v2:host=nuc2:project=default:status=None:type=None:o=0:l=50:scope=partner",
        "vms:list:v2:host=nuc2:project=default:status=None:o=0:l=50:scope=tenant",
        "projects:list:host=nuc2:scope=partner",
        "clusters:list:all:scope=partner",
        "regions:overview:v8:scope=partner",
    )
    for key in cached_keys:
        cache.set("api_responses", key, {"stale": True}, ttl=300)
    cache.set("api_responses", "unrelated:list:scope=all", {"keep": True}, ttl=300)

    assert cache.instance_list_generation() is None

    cache.invalidate_instance_cache()
    cache.invalidate_resource_cache("clusters", "projects", "regions")

    assert cache.instance_list_generation() is not None
    for key in cached_keys:
        assert cache.get("api_responses", key) is None, key
    assert cache.get("api_responses", "unrelated:list:scope=all") == {"keep": True}
