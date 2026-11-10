"""Where baselines live: one shared Redis store for every worker, a bounded local fallback."""

import logging
import threading

import pytest

from app.core import rate_sampler
from app.core.rate_sampler import (
    LocalSampleStore,
    RateSampler,
    Sample,
    SamplerPolicy,
    StoredWindow,
)
from tests._rate_sampler_fakes import FakeRedis

POLICY = SamplerPolicy(min_window=1.5, max_age=120.0, ttl_seconds=180)


@pytest.fixture
def shared_redis(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(rate_sampler, "_redis_client", lambda: redis)
    return redis


def _sample(t, cpu):
    return Sample(taken_at=t, epoch="e", counters={"cpu_ns": cpu})


def test_four_workers_sharing_redis_agree_after_one_seed(shared_redis):
    """One seed in the shared store serves every worker; none answers "calculating" again."""
    workers = [RateSampler("ns", POLICY) for _ in range(4)]

    first = workers[0].observe("i", _sample(100.0, 0.0))
    later = [w.observe("i", _sample(100.0 + 5 * (n + 1), 5e9 * (n + 1))) for n, w in enumerate(workers)]

    assert first.status == "calculating"
    assert [r.status for r in later] == ["ready"] * 4
    assert {round(r.rates["cpu_ns"]) for r in later} == {int(1e9)}


def test_compare_and_set_refuses_an_older_window(shared_redis):
    sampler = RateSampler("ns", POLICY)
    sampler.observe("i", _sample(100.0, 0.0))
    sampler.observe("i", _sample(110.0, 10e9))

    # A slow request that read at 104 stores nothing over the newer 110 baseline.
    stale = rate_sampler.StoredWindow(104.0, "e", {"cpu_ns": 4e9}, {}, None)
    store = rate_sampler.RedisSampleStore(shared_redis)
    store.put_many([(sampler.key("i"), stale)], 180)

    stored = StoredWindow.loads(shared_redis.hget(sampler.key("i"), "v"))
    assert stored is not None and stored.taken_at == 110.0
    assert shared_redis.ttl[sampler.key("i")] == 180


def test_keys_are_namespaced(shared_redis):
    RateSampler("instance_state", POLICY).observe("cluster:proj:name:uuid", _sample(1.0, 1.0))

    assert list(shared_redis.hashes) == ["orcastra:rate:v1:instance_state:cluster:proj:name:uuid"]


def test_redis_failure_falls_back_once_with_one_warning(shared_redis, caplog):
    sampler = RateSampler("ns", POLICY)
    shared_redis.fail_with = ConnectionError("redis down")

    with caplog.at_level(logging.WARNING, logger="app.core.rate_sampler"):
        first = sampler.observe("i", _sample(100.0, 0.0))
        second = sampler.observe("i", _sample(105.0, 5e9))

    assert first.status == "calculating"
    assert second.status == "ready", "the local store must carry the baseline during the outage"
    warnings = [r for r in caplog.records if "fell back" in r.getMessage()]
    assert len(warnings) == 1


def test_no_redis_uses_the_local_store():
    sampler = RateSampler("ns", POLICY)

    sampler.observe("i", _sample(100.0, 0.0))
    reading = sampler.observe("i", _sample(105.0, 5e9))

    assert reading.status == "ready"


def test_local_store_expires_and_stays_bounded(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(rate_sampler.time, "monotonic", lambda: clock[0])
    store = LocalSampleStore(max_entries=3)
    window = StoredWindow(1.0, "e", {}, {}, None)

    store.put_many([(f"k{n}", window) for n in range(5)], ttl_seconds=10)
    assert store.get_many(["k0", "k1", "k2", "k3", "k4"]) == [None, None, window, window, window]

    clock[0] += 11
    assert store.get_many(["k4"]) == [None]


def test_local_store_compare_and_set_under_threads():
    store = LocalSampleStore()
    barrier = threading.Barrier(8)

    def write(n):
        barrier.wait()
        store.put_many([("k", StoredWindow(float(n), "e", {}, {}, None))], ttl_seconds=60)

    threads = [threading.Thread(target=write, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert store.get_many(["k"])[0].taken_at == 7.0


def test_observe_many_reads_and_writes_in_one_pipeline_each(shared_redis):
    sampler = RateSampler("ns", POLICY)
    samples = {f"i{n}": _sample(100.0, float(n)) for n in range(50)}

    readings = sampler.observe_many(samples)

    assert len(readings) == 50
    assert all(r.status == "calculating" for r in readings.values())
    assert shared_redis.script_calls == 50
