"""The monitoring dashboard cache must be shared between workers, not per process.

It used to be a pair of module-level dicts with a 35s TTL. With WEB_CONCURRENCY=4 that meant
four independent snapshots: the background collector only ever warmed the worker it ran in, and
an operator refreshing the page hopped workers and watched the numbers jump backwards. The
arithmetic made it worse than stale, it made it useless: under a 10s client poll each worker
saw a request roughly every 40s, past its own 35s TTL, so the cache missed nearly every time
and every miss paid a full node fan-out.

Structural rather than a multi-process test, because the failure cannot be reproduced inside
one interpreter. What can be pinned is that no module state holds the body and that the read
path goes to the shared cache.
"""

import asyncio
from datetime import datetime, timezone

import pytest

from app.models.schemas import MonitoringDashboardResponse
from app.routers import monitoring


def test_no_module_state_holds_the_dashboard_body():
    """The regression guard. These names existing again means the split came back."""
    for leaked in ("_dashboard_cache", "_dashboard_cache_times", "_dashboard_cache_ttl"):
        assert not hasattr(monitoring, leaked), (
            f"{leaked} is per-process state; the dashboard body belongs in the shared cache"
        )


def test_the_cache_key_is_scope_suffixed():
    """Partner and admin must never share an entry."""
    assert monitoring.MONITORING_DASHBOARD_CACHE_KEY == "monitoring:dashboard:v2"
    assert monitoring._DASHBOARD_CACHE_TTL == 35


class _Cache:
    """Records what the handler asks of the shared cache."""

    def __init__(self, seeded=None):
        self.store = dict(seeded or {})
        self.reads = []
        self.writes = []

    def get(self, namespace, key):
        self.reads.append((namespace, key))
        return self.store.get((namespace, key))

    def set(self, namespace, key, value, ttl=None):
        self.writes.append((namespace, key, ttl))
        self.store[(namespace, key)] = value


class _ExplodingLXD:
    """Any attribute access means the handler tried to fan out on a cache hit."""

    def __getattr__(self, name):
        raise AssertionError(f"cache hit must not touch the cluster (tried {name!r})")


def test_a_cache_hit_is_served_from_the_shared_cache(monkeypatch):
    """A warm entry must short-circuit before any cluster contact."""
    body = MonitoringDashboardResponse(
        timestamp=datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc),
        total_nodes=3,
        online_nodes=2,
    ).model_dump()

    cache = _Cache({("api_responses", "monitoring:dashboard:v2:scope=all"): body})
    monkeypatch.setattr(monitoring, "get_cache_manager", lambda: cache)

    result = asyncio.run(
        monitoring.get_monitoring_dashboard(lxd_service=_ExplodingLXD(), fresh=False)
    )

    assert isinstance(result, MonitoringDashboardResponse)
    assert result.total_nodes == 3
    assert result.online_nodes == 2
    # The timestamp is the collection time carried in the cached body, not read time.
    assert result.timestamp == datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    assert ("api_responses", "monitoring:dashboard:v2:scope=all") in cache.reads
    assert cache.writes == [], "a cache hit must not rewrite the entry"


def test_a_forced_read_bypasses_the_shared_cache(monkeypatch):
    """fresh=true must skip the entry rather than be handed the body it asked to skip."""
    body = MonitoringDashboardResponse(
        timestamp=datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    ).model_dump()

    cache = _Cache({("api_responses", "monitoring:dashboard:v2:scope=all"): body})
    monkeypatch.setattr(monitoring, "get_cache_manager", lambda: cache)

    # The fan-out is not stubbed, so a bypass must get past the cache and fail on the cluster
    # call rather than return the cached body.
    with pytest.raises(Exception):
        asyncio.run(
            monitoring.get_monitoring_dashboard(lxd_service=_ExplodingLXD(), fresh=True)
        )

    assert ("api_responses", "monitoring:dashboard:v2:scope=all") not in cache.reads
