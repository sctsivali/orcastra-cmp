"""One history point per dashboard sweep and scope, whichever worker built it.

Each worker kept its own "last stored" time, so when several built the same dashboard within a
few seconds every one of them appended it. Over a day about 70% of the points were copies, and
the read-modify-write of the history value let concurrent appends overwrite each other.
"""

import asyncio
from typing import Any

from app.routers import monitoring
from app.services import monitoring_history
from app.services.monitoring_history import HISTORY_SLOT_SECONDS, claim_history_slot
from tests.test_monitoring_member_cache_generation_fences import (
    _DashboardService,
    _GenerationCache,
    _MetadataStore,
    _scoped,
)


class _SlotRedis:
    def __init__(self):
        self.now = 0.0
        self.keys: dict[str, float] = {}

    def set(self, key, value, nx=False, ex=None):
        expires = self.keys.get(key)
        if nx and expires is not None and expires > self.now:
            return None
        self.keys[key] = self.now + ex
        return True


class _Cache:
    def __init__(self, redis):
        self.redis_client = redis


def test_one_worker_wins_each_slot_of_a_scope():
    redis = _SlotRedis()
    cache = _Cache(redis)

    first = [claim_history_slot(cache, "scope-a", now=100.0, local={}) for _ in range(4)]
    other_scope = claim_history_slot(cache, "scope-b", now=100.0, local={})
    redis.now = HISTORY_SLOT_SECONDS + 1
    next_slot = claim_history_slot(cache, "scope-a", now=121.0, local={})

    assert first == [True, False, False, False]
    assert other_scope is True, "scopes never share a slot"
    assert next_slot is True


def test_without_redis_each_process_still_spaces_its_points():
    local: dict[str, float] = {}

    assert claim_history_slot(_Cache(None), "scope-a", now=100.0, local=local) is True
    assert claim_history_slot(_Cache(None), "scope-a", now=110.0, local=local) is False
    assert claim_history_slot(_Cache(None), "scope-a", now=121.0, local=local) is True


def test_a_failing_redis_falls_back_to_the_process_slot():
    class _Broken:
        def set(self, *args, **kwargs):
            raise RuntimeError("connection reset")

    local: dict[str, float] = {}
    assert claim_history_slot(_Cache(_Broken()), "scope-a", now=100.0, local=local) is True
    assert claim_history_slot(_Cache(_Broken()), "scope-a", now=101.0, local=local) is False


def _dashboard(monkeypatch, cache: _GenerationCache, service: Any) -> None:
    monkeypatch.setattr(monitoring, "get_cache_manager", lambda: cache)
    monkeypatch.setattr(monitoring, "get_scope_cache_key", _scoped)
    monkeypatch.setattr(monitoring, "get_node_metadata_store", lambda: _MetadataStore())
    monkeypatch.setattr(monitoring, "tenant_acl_active", lambda: False)
    asyncio.run(monitoring.get_monitoring_dashboard(lxd_service=service, fresh=True))


def _history_writes(cache: _GenerationCache) -> list:
    return [write for write in cache.writes if write[0] == monitoring_history.HISTORY_NAMESPACE]


def test_two_sweeps_inside_one_slot_write_one_point(monkeypatch):
    monkeypatch.setattr(monitoring, "_last_history_store_times", {})
    cache = _GenerationCache()

    _dashboard(monkeypatch, cache, _DashboardService(cache, advance=False))
    _dashboard(monkeypatch, cache, _DashboardService(cache, advance=False))

    assert len(_history_writes(cache)) == 1


def test_a_sweep_no_cluster_answered_writes_no_point(monkeypatch):
    class _Silent(_DashboardService):
        async def list_all_vms(self):
            return [], []

    monkeypatch.setattr(monitoring, "_last_history_store_times", {})
    cache = _GenerationCache()

    _dashboard(monkeypatch, cache, _Silent(cache, advance=False))

    assert _history_writes(cache) == [], "an all-zero point reads as an idle fleet"
