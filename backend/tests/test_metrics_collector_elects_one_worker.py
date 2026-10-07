"""Only one worker may run the expensive collection sweep, and it keeps the job while alive.

The collector is started from the FastAPI lifespan, which runs once per worker. With
WEB_CONCURRENCY=4 that is four collectors. A lease taken per cycle with a 30 s expiry elected
nobody for the rest of a cycle that lasted the sweep plus 30 s, so another worker took it
mid-cycle and swept again: over a day the dashboard was built three to four times per cycle.

These tests pin the held lease: one leader, kept by a heartbeat, replaced within one lease once
it stops beating, handed back on shutdown, and a deployment without Redis keeps collecting rather
than going silent, because a collector that elects nobody is worse than one that over-collects.
"""

import asyncio
import time

from app.routers import monitoring
from app.services import collector_leadership
from app.services.metrics_collector import MetricsCollector


class _FakeRedis:
    """SET NX EX, GET, EXPIRE and DEL with a hand-driven clock, behind the two lease scripts."""

    def __init__(self):
        self.now = 0.0
        self.store = {}

    def _live(self, key):
        entry = self.store.get(key)
        if entry is None or entry[1] <= self.now:
            self.store.pop(key, None)
            return None
        return entry[0]

    def register_script(self, source):
        if source == collector_leadership._CLAIM:
            return self._claim
        if source == collector_leadership._RELEASE:
            return self._release
        raise AssertionError("an unexpected script was registered")

    def _claim(self, keys, args):
        key, (token, seconds) = keys[0], args
        holder = self._live(key)
        if holder is None or holder == token:
            self.store[key] = (token, self.now + int(seconds))
            return 1
        return 0

    def _release(self, keys, args):
        key, (token,) = keys[0], args
        if self._live(key) == token:
            del self.store[key]
            return 1
        return 0


class _Cache:
    def __init__(self, redis_client):
        self.redis_client = redis_client


def _workers(monkeypatch, redis, count=4):
    monkeypatch.setattr("app.core.cache.get_cache_manager", lambda: _Cache(redis), raising=True)
    return [MetricsCollector(collection_interval=30) for _ in range(count)]


def test_exactly_one_worker_wins_a_cycle(monkeypatch):
    """Four workers racing the same cycle: one sweep, three relays."""
    workers = _workers(monkeypatch, _FakeRedis())

    verdicts = [worker._claim_collection_lease() for worker in workers]

    assert verdicts.count(True) == 1, f"expected one leader, got {verdicts}"


def test_a_heartbeat_keeps_the_lease_through_long_sweeps(monkeypatch):
    """Every worker beats every 5 s: the leader's beats renew, the followers' never win."""
    redis = _FakeRedis()
    leader, *followers = _workers(monkeypatch, redis)
    assert leader._claim_collection_lease() is True

    beat = MetricsCollector.HEARTBEAT_SECONDS
    while redis.now < 300:
        redis.now += beat
        assert leader._claim_collection_lease() is True, "a beating leader keeps the job"
        assert [f._claim_collection_lease() for f in followers] == [False] * 3


def test_the_lease_survives_missed_beats_but_not_a_dead_leader():
    assert MetricsCollector.LEASE_SECONDS >= 3 * MetricsCollector.HEARTBEAT_SECONDS
    assert MetricsCollector.LEASE_SECONDS <= 30, "a dead leader must be replaced within seconds"


def test_the_collected_dashboard_outlives_a_cycle(monkeypatch):
    collector = _workers(monkeypatch, _FakeRedis(), count=1)[0]

    assert monitoring.COLLECTED_DASHBOARD_TTL >= 3 * collector.collection_interval, (
        "followers and admins must find the leader's dashboard for a whole sweep plus pause, "
        "and while a lapsed lease is taken over"
    )


def test_starting_beats_and_stopping_cancels_the_heartbeat(monkeypatch):
    redis = _FakeRedis()
    collector = _workers(monkeypatch, redis, count=1)[0]

    async def _idle_loop():
        await asyncio.sleep(3600)

    monkeypatch.setattr(collector, "_collection_loop", _idle_loop)

    async def _run():
        await collector.start_async(lxd_service=object())
        await asyncio.sleep(0)
        assert collector._leading is True, "the first beat claims at once"
        heartbeat = collector._heartbeat_task
        collector.stop()
        await asyncio.sleep(0)
        return heartbeat

    heartbeat = asyncio.run(_run())

    assert heartbeat.cancelled()
    assert redis.store == {}, "a clean stop hands the lease back"


def test_a_follower_that_takes_over_sweeps_at_once(monkeypatch):
    collector = _workers(monkeypatch, _FakeRedis(), count=1)[0]
    collector._running = True
    collector._leading = False
    collector.RELAY_POLL_SECONDS = 0.01

    async def _relay():
        collector._leading = True  # the heartbeat took over the lapsed lease meanwhile

    monkeypatch.setattr(collector._dashboards, "relay", _relay)
    started = time.monotonic()

    asyncio.run(collector._relay_until_next_cycle())

    assert time.monotonic() - started < 1.0, "a new leader must not wait out a follower's pause"


def test_a_dead_leader_is_replaced_once_its_lease_lapses(monkeypatch):
    redis = _FakeRedis()
    leader, successor = _workers(monkeypatch, redis, count=2)
    leader._claim_collection_lease()

    redis.now = MetricsCollector.LEASE_SECONDS - 1
    assert successor._claim_collection_lease() is False
    redis.now = MetricsCollector.LEASE_SECONDS + 1
    assert successor._claim_collection_lease() is True
    assert leader._claim_collection_lease() is False, "the old leader steps down"


def test_stopping_hands_the_lease_back_and_only_its_own(monkeypatch):
    redis = _FakeRedis()
    leader, successor = _workers(monkeypatch, redis, count=2)
    leader._claim_collection_lease()

    successor.stop()
    assert successor._claim_collection_lease() is False, "a follower released nothing"

    leader.stop()
    assert successor._claim_collection_lease() is True


def test_no_redis_means_collect_locally(monkeypatch):
    """Single-worker and Redis-down deployments must not stop collecting."""
    collector = _workers(monkeypatch, None, count=1)[0]
    assert collector._claim_collection_lease() is True


def test_a_failing_redis_means_collect_locally(monkeypatch):
    """A lease we cannot take is not a reason to skip the work."""

    class _Broken:
        def register_script(self, *args, **kwargs):
            raise RuntimeError("connection reset")

    collector = _workers(monkeypatch, _Broken(), count=1)[0]
    assert collector._claim_collection_lease() is True
