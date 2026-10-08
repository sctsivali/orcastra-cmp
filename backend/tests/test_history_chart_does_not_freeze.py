"""The metrics history chart must keep moving after the store reaches its point cap.

The history endpoint reuses an encoded body while its selection is unchanged, so a repeated read
does not rebuild up to 8640 points. That memo was once versioned on the number of points. History
is capped at 8640, so the moment the buffer filled the count stopped changing and every request
matched the cached entry: the chart froze until a restart, roughly 9.6 hours after startup at
the 4 s store interval of the time.

The sorted-set store versions a selection on its size, the points inside it and the oldest and
newest scores, so a rotation at the cap (one point in, the oldest out) is a new version. These
tests pin both halves at the real cap, through the endpoint: a saturated store that gains a
point answers it, and an unchanged store answers the same body without fetching a member.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.core.cache import CacheManager
from app.routers import monitoring
from app.services import monitoring_history_store as store
from app.services.monitoring_history import HISTORY_MAX_POINTS, history_key
from tests._fake_zset_redis import FakeRedis

SCOPE = "dashboard:scope=all"


def _point(at: datetime, seq: int) -> dict:
    return {
        "timestamp": at.isoformat(),
        "cpu_usage_percent": float(seq % 100),
        "memory_usage_percent": 50.0,
        "storage_usage_percent": 10.0,
        "running_instances": 3,
    }


@pytest.fixture
def saturated(monkeypatch):
    redis = FakeRedis()
    cache = CacheManager(redis_url=None)
    cache.redis_client = redis
    monkeypatch.setattr(monitoring, "get_cache_manager", lambda: cache)
    monkeypatch.setattr(monitoring, "get_scope_cache_key", lambda _name: SCOPE)
    # note: 8640 points 9 s apart span 21.6 hours; the Postgres refill is not under test here.
    monkeypatch.setattr(monitoring, "needs_hydrate", lambda _read: False)
    now = datetime.now(timezone.utc)
    points = [_point(now - timedelta(seconds=9 * (HISTORY_MAX_POINTS - i)), i) for i in range(HISTORY_MAX_POINTS)]
    now_ms = int(now.timestamp() * 1000)
    asyncio.run(
        store._run(
            store._write_blocking, cache, history_key(SCOPE), store._rows(points, now_ms), now_ms,
            label="test", timeout=30.0,
        )
    )
    assert redis.zcard(store.points_key(history_key(SCOPE))) == HISTORY_MAX_POINTS
    return cache, now


def _read(points: int = 60):
    response = asyncio.run(monitoring.get_metrics_history(window=None, points=points, lxd_service=None))
    return response.body


def _newest(body: bytes) -> str:
    return json.loads(body)[-1]["timestamp"]


def test_a_saturated_store_still_reports_new_points(saturated):
    """The regression. Same size, newer content, and a size-versioned memo answered the old body."""
    cache, now = saturated
    first = _read()
    assert len(json.loads(first)) == 60

    # Exactly what the collector does at the cap: one point in, the oldest out.
    newest = now + timedelta(seconds=1)
    assert asyncio.run(store.record_point(cache, history_key(SCOPE), _point(newest, HISTORY_MAX_POINTS)))
    assert cache.redis_client.zcard(store.points_key(history_key(SCOPE))) == HISTORY_MAX_POINTS

    second = _read()

    assert _newest(second) != _newest(first)
    assert datetime.fromisoformat(_newest(second).replace("Z", "+00:00")) == newest


def test_an_unchanged_store_is_served_from_the_memo(saturated):
    """The memo must still do its job, or the fix trades a freeze for a rebuild per request."""
    cache, _ = saturated

    first = _read()
    cache.redis_client.calls.clear()
    second = _read()

    # The same bytes object, not merely equal bytes: proof no rebuild happened.
    assert second is first
    assert "zrange" in cache.redis_client.calls, "the version was read"
    assert cache.redis_client.calls.count("zrange") == 2, "only the oldest and newest, no member fetch"
