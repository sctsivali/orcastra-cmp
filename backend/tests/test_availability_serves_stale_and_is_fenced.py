"""Cluster availability under stale-while-revalidate: fenced against registry changes, and a
plain peek never serves an entry past its TTL.
"""

import asyncio

from app.core import cache as cache_module
from app.core.cache import (
    NODES_AVAILABILITY_CACHE_KEY,
    NODES_STATUS_CACHE_KEY,
    CacheManager,
)
from app.core.cache_envelope import wrap
from app.core.org_scope import get_scope_cache_key
from app.services.lxd import status as lxd_status
from tests.test_availability_never_omits_a_cluster import ALL, _Service


def _manager(monkeypatch) -> CacheManager:
    manager = CacheManager(redis_url=None)
    monkeypatch.setattr(cache_module, "_global_cache", manager)
    monkeypatch.setattr(lxd_status, "get_cache_manager", lambda: manager)
    return manager


def test_a_registration_during_the_probe_keeps_its_result_out(monkeypatch):
    manager = _manager(monkeypatch)
    real_probe = lxd_status.probe_clusters

    def probe_while_a_cluster_registers(*args, **kwargs):
        manager.invalidate_availability_cache()
        return real_probe(*args, **kwargs)

    monkeypatch.setattr(lxd_status, "probe_clusters", probe_while_a_cluster_registers)
    response = asyncio.run(lxd_status.resolve_availability(_Service()))
    assert {row.name for row in response.clusters} == set(ALL)
    key = get_scope_cache_key(NODES_AVAILABILITY_CACHE_KEY)
    assert manager.get("api_responses", key) is None, (
        "a probe that a registry change overtook must not be remembered"
    )


def test_a_clean_probe_is_cached_and_the_next_read_is_a_hit(monkeypatch):
    manager = _manager(monkeypatch)
    first = asyncio.run(lxd_status.resolve_availability(_Service()))
    assert first.source == "probe"
    again = asyncio.run(lxd_status.resolve_availability(_Service()))
    assert again.source == "availability_cache"
    assert again.generated_at == first.generated_at, "a cached body keeps the time it was built"


def test_peek_ignores_entries_past_their_ttl(monkeypatch):
    manager = _manager(monkeypatch)
    old = 1_000.0
    key = get_scope_cache_key(NODES_AVAILABILITY_CACHE_KEY)
    body = asyncio.run(lxd_status.resolve_availability(_Service())).model_dump(mode="json")
    manager.set("api_responses", key, wrap(body, stored_at=old, build_started_at=old, fresh_for=45), ttl=225)
    nodes_key = get_scope_cache_key(NODES_STATUS_CACHE_KEY)
    manager.set(
        "api_responses",
        nodes_key,
        wrap({"nodes": [], "all_connected": True}, stored_at=old, build_started_at=old, fresh_for=180),
        ttl=360,
    )
    assert lxd_status.peek_availability() is None, (
        "a reader outside swr.serve() never refreshes, so it must not serve a stale entry"
    )
