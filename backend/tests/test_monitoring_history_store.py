"""The monitoring history store: one sorted-set member per point, scored by its timestamp.

One JSON string per scope would be read and rewritten whole on every append (megabytes for the
admin scope) and parsed on the event loop on every chart read. These tests pin the store's rules:
a write never duplicates a timestamp, retention is 24 hours and a point cap, a window selects by
time, an unchanged selection is served without fetching a member, Redis trouble falls back to a
bounded per-process buffer, and the previous string key and Postgres merge in without overwriting
anything.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core.blocking import BlockingPoolFull
from app.core.cache import CacheManager
from app.services import metrics_history_service
from app.services import monitoring_history_store as store
from app.services.monitoring_history import HISTORY_NAMESPACE, HISTORY_TTL_SECONDS
from tests._fake_zset_redis import FakeRedis

KEY = "monitoring:history:scope=all"
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    """The store reads the wall clock for windows and retention; pin it to NOW."""
    monkeypatch.setattr(store, "time", SimpleNamespace(time=lambda: NOW.timestamp()))


def _cache(redis=None):
    manager = CacheManager(redis_url=None)
    manager.redis_client = redis
    return manager


def _point(seconds_ago: float, **values):
    point = {
        "timestamp": (NOW - timedelta(seconds=seconds_ago)).isoformat(),
        "cpu_usage_percent": 12.5,
        "memory_usage_percent": 40.0,
        "storage_usage_percent": 7.0,
        "running_instances": 3,
    }
    point.update(values)
    return point


def _record(cache, *points):
    return [asyncio.run(store.record_point(cache, KEY, point)) for point in points]


def _read(cache, window=None, points=None):
    return asyncio.run(store.history_body(cache, KEY, window_seconds=window, points=points))


def _timestamps(read):
    return [datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")) for row in json.loads(read.body)]


def _at(seconds_ago: float) -> datetime:
    return NOW - timedelta(seconds=seconds_ago)


def test_a_timestamp_scores_to_its_exact_millisecond():
    assert store.score_of({"timestamp": "2026-10-08T12:00:00.123999+00:00"}) == 1791460800123
    assert store.score_of({"timestamp": "2026-10-08T12:00:00Z"}) == 1791460800000
    assert store.score_of({"timestamp": "2026-10-08T12:00:00"}) == 1791460800000, "naive reads as UTC"
    assert store.score_of({"timestamp": "yesterday"}) is None
    assert store.score_of({}) is None and store.score_of("junk") is None


def test_each_point_is_one_member_and_a_repeated_timestamp_is_not_stored_twice():
    redis = FakeRedis()
    cache = _cache(redis)

    written = _record(cache, _point(60), _point(30), _point(30, cpu_usage_percent=99.0))

    assert written == [True, True, False]
    members = redis.zsets[store.points_key(KEY)]
    assert len(members) == 2
    assert all(json.loads(member)["cpu_usage_percent"] == 12.5 for member in members)
    assert redis.ttl[store.points_key(KEY)] == HISTORY_TTL_SECONDS


def test_a_write_drops_what_fell_out_of_24_hours_and_past_the_point_cap(monkeypatch):
    redis = FakeRedis()
    cache = _cache(redis)
    key = store.points_key(KEY)
    redis.zsets[key] = {json.dumps(_point(25 * 3600)): (NOW.timestamp() - 25 * 3600) * 1000}

    _record(cache, _point(90))
    assert len(redis.zsets[key]) == 1, "the 25-hour-old point is trimmed by the write"

    monkeypatch.setattr(store, "HISTORY_MAX_POINTS", 3)
    _record(cache, _point(60), _point(30), _point(0))

    assert _timestamps(_read(cache, points=10)) == [_at(s) for s in (60, 30, 0)]


def test_a_point_older_than_the_retention_is_never_written():
    redis = FakeRedis()

    assert _record(_cache(redis), _point(24 * 3600 + 1)) == [False]
    assert store.points_key(KEY) not in redis.zsets


def test_a_window_selects_by_time_and_the_legacy_count_by_points():
    cache = _cache(FakeRedis())
    asyncio.run(
        store._run(
            store._write_blocking, cache, KEY,
            store._rows([_point(30 * i) for i in range(200)], int(NOW.timestamp() * 1000)),
            int(NOW.timestamp() * 1000), label="test", timeout=5.0,
        )
    )

    five_minutes = _read(cache, window=300)
    legacy = _read(cache, points=60)

    assert len(json.loads(five_minutes.body)) == 11, "one point per 30 s: 5m is 11 points, not 60"
    assert _timestamps(five_minutes)[-1] == _timestamps(legacy)[-1]
    assert len(json.loads(legacy.body)) == 60
    assert five_minutes.total == 200 and five_minutes.store_ok
    assert len(json.loads(_read(cache).body)) == 11, "no parameter means the 5-minute window"


def test_an_unchanged_selection_is_served_without_fetching_a_member():
    redis = FakeRedis()
    cache = _cache(redis)
    _record(cache, _point(60), _point(30))

    first = _read(cache, window=300)
    redis.calls.clear()
    second = _read(cache, window=300)

    assert second.body is first.body
    assert "zrangebyscore" not in redis.calls and "zrange" in redis.calls


def test_a_saturated_store_that_rotates_still_serves_the_new_point(monkeypatch):
    """At the point cap the count stays fixed; the memo must still change when points rotate."""
    monkeypatch.setattr(store, "HISTORY_MAX_POINTS", 3)
    cache = _cache(FakeRedis())
    _record(cache, _point(90), _point(60), _point(30))
    before = _read(cache, window=3600)

    _record(cache, _point(0))
    after = _read(cache, window=3600)

    assert before.total == after.total == 3
    assert _timestamps(after)[-1] == _at(0)
    assert _timestamps(after)[0] == _at(60)


def test_a_point_leaving_the_window_changes_the_body(monkeypatch):
    cache = _cache(FakeRedis())
    _record(cache, _point(290), _point(10))
    assert len(json.loads(_read(cache, window=300).body)) == 2

    later = NOW + timedelta(seconds=20)
    monkeypatch.setattr(store, "time", SimpleNamespace(time=lambda: later.timestamp()))

    assert _timestamps(_read(cache, window=300)) == [_at(10)]


def test_an_unreadable_member_is_skipped_not_fatal():
    redis = FakeRedis()
    cache = _cache(redis)
    _record(cache, _point(30))
    redis.zsets[store.points_key(KEY)]["{not json"] = (NOW.timestamp() - 20) * 1000

    assert len(json.loads(_read(cache, window=300).body)) == 1


def test_redis_trouble_keeps_points_in_this_worker_and_says_so():
    redis = FakeRedis()
    redis.fail_with = ConnectionError("connection refused")
    cache = _cache(redis)

    assert _record(cache, _point(30), _point(0)) == [True, True]
    read = _read(cache, window=300)

    assert len(json.loads(read.body)) == 2
    assert read.store_ok is False, "refills would only time out too"


def test_without_redis_the_process_buffer_is_the_store():
    cache = _cache(None)
    _record(cache, _point(30))

    read = _read(cache, window=300)

    assert read.total == 1 and read.store_ok is True


def test_the_process_buffer_is_bounded_in_points_and_in_scopes(monkeypatch):
    monkeypatch.setattr(store, "LOCAL_MAX_POINTS", 2)
    monkeypatch.setattr(store, "LOCAL_MAX_SCOPES", 2)
    local = store.LocalPoints()
    now_ms = int(NOW.timestamp() * 1000)

    local.write("a", [(now_ms - 3000, "1"), (now_ms - 2000, "2"), (now_ms - 1000, "3")], now_ms=now_ms)
    local.write("b", [(now_ms, "x")], now_ms=now_ms)
    local.write("c", [(now_ms, "y")], now_ms=now_ms)

    assert local.select("a", since_ms=None, last=None) == [], "the oldest scope went first"
    assert local.select("c", since_ms=None, last=None) == ["y"]
    local.write("b", [(now_ms - 5, "older"), (now_ms - 4, "old"), (now_ms, "dupe")], now_ms=now_ms)
    assert local.select("b", since_ms=None, last=None) == ["old", "x"]


def test_a_refused_write_reports_false_and_never_raises(monkeypatch):
    async def full(*_args, **_kwargs):
        raise BlockingPoolFull("monitoring.history.write", 16)

    monkeypatch.setattr(store, "run_blocking", full)

    assert asyncio.run(store.record_point(_cache(FakeRedis()), KEY, _point(0))) is False


def test_reads_and_writes_never_share_an_admission_bound(monkeypatch):
    seen = []

    async def recorder(fn, *args, **options):
        seen.append((options["label"], options["executor"], options["max_in_flight"]))
        return fn(*args)

    monkeypatch.setattr(store, "run_blocking", recorder)
    cache = _cache(FakeRedis())
    asyncio.run(store.record_point(cache, KEY, _point(0)))
    _read(cache)

    (write_label, write_pool, _), (read_label, read_pool, read_cap) = seen
    assert write_pool is store._write_executor and read_pool is store._read_executor
    assert read_cap >= 32, "a burst of chart reads is served, not refused"


def _legacy(cache, points):
    cache.redis_client.strings[f"orcastra:{HISTORY_NAMESPACE}:{KEY}"] = json.dumps(points)


def test_the_previous_string_key_is_imported_once_and_merges_by_timestamp():
    redis = FakeRedis()
    cache = _cache(redis)
    _record(cache, _point(30))
    _legacy(cache, [_point(90), _point(60), _point(30, cpu_usage_percent=1.0), "junk", _point(30 * 3600)])

    first = asyncio.run(store.import_previous_history(cache, KEY))
    redis.calls.clear()
    again = asyncio.run(store.import_previous_history(cache, KEY))

    assert first == 2, "the 30 s point was already stored and the 30-hour one is past retention"
    assert again == 0 and redis.calls == [], "a process asks once per scope"
    assert redis.strings[f"{store.IMPORT_DONE_PREFIX}:{KEY}"] == "1"
    assert [row["cpu_usage_percent"] for row in json.loads(_read(cache, window=300).body)] == [12.5] * 3


def test_another_worker_importing_means_ask_again_later():
    redis = FakeRedis()
    cache = _cache(redis)
    redis.strings[f"{store.IMPORT_LOCK_PREFIX}:{KEY}"] = "1"
    _legacy(cache, [_point(60)])

    assert asyncio.run(store.import_previous_history(cache, KEY)) == 0
    assert KEY not in store._imported

    del redis.strings[f"{store.IMPORT_LOCK_PREFIX}:{KEY}"]
    assert asyncio.run(store.import_previous_history(cache, KEY)) == 1


def test_a_finished_import_elsewhere_is_not_repeated():
    redis = FakeRedis()
    cache = _cache(redis)
    redis.strings[f"{store.IMPORT_DONE_PREFIX}:{KEY}"] = "1"
    _legacy(cache, [_point(60)])

    assert asyncio.run(store.import_previous_history(cache, KEY)) == 0
    assert store.points_key(KEY) not in redis.zsets


def test_postgres_fills_only_the_missing_timestamps_and_once_per_lock(monkeypatch):
    calls = []

    async def load(points, scope, since):
        calls.append((points, scope, since))
        return [_point(120), _point(90, cpu_usage_percent=55.0), _point(60)]

    monkeypatch.setattr(metrics_history_service, "load_metrics_history", load)
    redis = FakeRedis()
    cache = _cache(redis)
    _record(cache, _point(90))

    added = asyncio.run(store.hydrate_from_postgres(cache, KEY))
    again = asyncio.run(store.hydrate_from_postgres(cache, KEY))

    assert (added, again) == (2, 0)
    assert len(calls) == 1, "the lock holds the next worker off for its period"
    points, scope, since = calls[0]
    assert (points, scope) == (store.HISTORY_MAX_POINTS, "all")
    assert abs((datetime.now(timezone.utc) - since) - timedelta(hours=24)) < timedelta(minutes=1)
    assert [row["cpu_usage_percent"] for row in json.loads(_read(cache, window=300).body)] == [12.5] * 3


def test_a_failing_postgres_read_leaves_the_store_alone(monkeypatch):
    async def broken(**_kwargs):
        raise RuntimeError("database is starting up")

    monkeypatch.setattr(metrics_history_service, "load_metrics_history", broken)

    assert asyncio.run(store.hydrate_from_postgres(_cache(FakeRedis()), KEY)) == 0


def test_hydrate_is_wanted_only_when_the_store_starts_well_inside_the_retention():
    now_ms = int(NOW.timestamp() * 1000)
    day_ms = 24 * 3600 * 1000

    def read(oldest):
        return store.HistoryRead(b"[]", 0 if oldest is None else 1, oldest)

    assert store.needs_hydrate(read(None), now_ms=now_ms)
    assert store.needs_hydrate(read(now_ms - 3600 * 1000), now_ms=now_ms), "an hour after a Redis restart"
    assert not store.needs_hydrate(read(now_ms - day_ms + 60_000), now_ms=now_ms), "a full day"


def test_without_redis_postgres_is_still_read_once_per_lock_period(monkeypatch):
    calls = []

    async def load(points, scope, since):
        calls.append(since)
        return [_point(60)]

    monkeypatch.setattr(metrics_history_service, "load_metrics_history", load)
    cache = _cache(None)

    first = asyncio.run(store.hydrate_from_postgres(cache, KEY))
    second = asyncio.run(store.hydrate_from_postgres(cache, KEY))
    later = NOW + timedelta(seconds=store.LOCK_SECONDS + 1)
    monkeypatch.setattr(store, "time", SimpleNamespace(time=lambda: later.timestamp()))
    third = asyncio.run(store.hydrate_from_postgres(cache, KEY))

    assert (first, second, third) == (1, 0, 0)
    assert len(calls) == 2, "the lock expires, then the next read may refill again"
