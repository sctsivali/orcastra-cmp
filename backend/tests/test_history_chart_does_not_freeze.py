"""The metrics history chart must keep moving after the ring buffer saturates.

The history endpoint memoises its Pydantic models so a repeated read does not rebuild up to
8640 objects. That memo used to be versioned on len(history). History is capped at 8640
points, so the moment the ring buffer fills, the length stops changing forever and every
subsequent request matched the cached entry. The chart froze permanently: no TTL could
release it, only a process restart. At the 4s minimum store interval that is roughly 9.6
hours of uptime before the graph dies, which is why it survived review and short test runs.

The fix versions on the newest point instead. These tests pin both halves: a saturated buffer
that gains a point must produce a new response, and an unchanged buffer must still be served
from the memo rather than rebuilt.
"""

import asyncio
from datetime import datetime

import pytest

from app.routers import monitoring


HISTORY_CAP = 8640


def _point(seq: int) -> dict:
    """One history point. The timestamp is what identifies it, exactly as in production."""
    return {
        "timestamp": f"2026-08-26T12:00:{seq % 60:02d}.{seq:06d}+00:00",
        "cpu_usage_percent": float(seq % 100),
        "memory_usage_percent": 50.0,
        "storage_usage_percent": 10.0,
        "network_rx_bytes": seq,
        "network_tx_bytes": seq,
        "running_instances": 3,
    }


class _Cache:
    """Only the two calls the history read path makes."""

    def __init__(self, history):
        self.history = history

    def get(self, namespace, key):
        if namespace == "monitoring_history":
            return self.history
        return None

    def set(self, namespace, key, value, ttl=None):  # pragma: no cover - not exercised
        self.history = value


@pytest.fixture(autouse=True)
def _isolated_response_cache(monkeypatch):
    """The memo is module state shared by every test in the process."""
    monkeypatch.setattr(monitoring, "_history_response_cache", {})


def _read(monkeypatch, cache, points=60):
    monkeypatch.setattr(monitoring, "get_cache_manager", lambda: cache)
    monkeypatch.setattr(monitoring, "get_scope_cache_key", lambda _name: "dashboard:scope=all")
    return asyncio.run(monitoring.get_metrics_history(points=points, lxd_service=None))


def test_a_saturated_buffer_still_reports_new_points(monkeypatch):
    """The regression. Same length, newer content, and the old code returned the old list."""
    history = [_point(i) for i in range(HISTORY_CAP)]
    cache = _Cache(history)

    first = _read(monkeypatch, cache)
    assert len(first) == 60

    # Exactly what the collector does at the cap: append one, drop the oldest. The length is
    # unchanged, so a length-versioned memo cannot tell that anything happened.
    cache.history = history[1:] + [_point(HISTORY_CAP)]
    assert len(cache.history) == HISTORY_CAP

    second = _read(monkeypatch, cache)

    # note: MetricsHistoryPoint.timestamp is a datetime, so compare parsed values.
    assert second[-1].timestamp != first[-1].timestamp
    assert second[-1].timestamp == datetime.fromisoformat(_point(HISTORY_CAP)["timestamp"])


def test_an_unchanged_buffer_is_served_from_the_memo(monkeypatch):
    """The memo must still do its job, or the fix trades a freeze for 8640 rebuilds a request."""
    cache = _Cache([_point(i) for i in range(HISTORY_CAP)])

    first = _read(monkeypatch, cache)
    second = _read(monkeypatch, cache)

    # Same list object, not merely an equal one: proof no rebuild happened.
    assert first is second


def test_the_version_token_tracks_the_newest_point():
    """Length alone is not identity. Both halves of the token are load-bearing."""
    saturated = [_point(i) for i in range(HISTORY_CAP)]
    rotated = saturated[1:] + [_point(HISTORY_CAP)]

    assert len(saturated) == len(rotated)
    assert monitoring._history_version(saturated) != monitoring._history_version(rotated)

    # Growth before the cap must also register, which length alone did handle.
    assert monitoring._history_version(saturated) != monitoring._history_version(saturated[:-1])
    assert monitoring._history_version([]) == "0:"
