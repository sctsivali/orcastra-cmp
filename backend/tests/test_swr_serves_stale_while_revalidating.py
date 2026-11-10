"""core/swr.py: serve the last answer at once, replace it in the background, say which it served.

Every wait here ends on an event; the clock is the module's own and is moved by the test.
"""

import asyncio
import contextvars

import pytest

from app.core import org_scope, single_flight, swr
from app.core.cache import CacheManager
from app.core.cache_envelope import is_envelope, wrap
from app.core.request_context import RequestContext, set_request_context
from tests._loop_probes import SAFETY_TIMEOUT

BASE = "things:list:v1"


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def time(self) -> float:
        return self.now


class _Builder:
    """A build that counts its calls and, when gated, waits for the test to release it."""

    def __init__(self, tag="a", fresh_for=30, gated=False) -> None:
        self.tag = tag
        self.calls = 0
        self.fresh_for = fresh_for
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if not gated:
            self.release.set()
        self.scopes = []

    async def __call__(self) -> swr.Built:
        self.calls += 1
        n = self.calls
        self.scopes.append(org_scope.get_current_allowed_clusters())
        self.entered.set()
        await asyncio.wait_for(self.release.wait(), SAFETY_TIMEOUT)
        body = {"tag": self.tag, "n": n}
        return swr.Built(value=body, body=body, fresh_for=self.fresh_for)


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(swr, "time", fake)
    return fake


@pytest.fixture
def cache():
    return CacheManager(redis_url=None)


@pytest.fixture(autouse=True)
def _clean():
    single_flight._locks.clear()
    single_flight._waiters.clear()
    single_flight._last_rebuild.clear()
    swr._refreshing.clear()
    org_scope.set_allowed_clusters(None)
    yield
    for task in swr._refreshing.values():
        task.cancel()
    swr._refreshing.clear()


def _key() -> str:
    return org_scope.get_scope_cache_key(BASE)


def _stored(cache) -> dict:
    raw, _ = cache.get_entry(swr.NAMESPACE, _key())
    return raw


async def _settle() -> None:
    tasks = list(swr._refreshing.values())
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), SAFETY_TIMEOUT)


async def _serve(cache, build, fresh=False, generation=None):
    return await swr.serve(
        cache=cache, base_key=BASE, fresh=fresh, build=build, generation=generation, label="things"
    )


async def test_a_miss_builds_once_and_stores_an_envelope(cache, clock):
    build = _Builder()
    served = await _serve(cache, build)
    assert served.state == swr.MISS and served.value == {"tag": "a", "n": 1}
    raw = _stored(cache)
    assert is_envelope(raw) and raw["v"] == {"tag": "a", "n": 1}
    assert raw["s"] == 30 and raw["t"] == clock.now
    # Plain readers keep getting the body.
    assert cache.get(swr.NAMESPACE, _key()) == {"tag": "a", "n": 1}


async def test_a_fresh_entry_is_served_without_reading_the_clusters(cache, clock):
    build = _Builder()
    await _serve(cache, build)
    clock.now += 29
    served = await _serve(cache, build)
    assert served.state == swr.HIT and served.body == {"tag": "a", "n": 1} and build.calls == 1
    assert served.age == pytest.approx(29)


async def test_a_stale_entry_is_served_at_once_and_replaced_by_one_background_read(cache, clock):
    await _serve(cache, _Builder())
    clock.now += 31
    build = _Builder(tag="b", gated=True)
    served = await asyncio.gather(*[_serve(cache, build) for _ in range(5)])
    assert [s.state for s in served] == [swr.STALE] * 5
    assert all(s.body == {"tag": "a", "n": 1} for s in served)
    assert served[0].age == pytest.approx(31)
    await asyncio.wait_for(build.entered.wait(), SAFETY_TIMEOUT)
    assert build.calls == 1, "five stale readers must start one background read"
    build.release.set()
    await _settle()
    assert cache.get(swr.NAMESPACE, _key()) == {"tag": "b", "n": 1}
    assert swr._refreshing == {}
    again = await _serve(cache, build)
    assert again.state == swr.HIT


async def test_past_the_stale_window_the_caller_waits_for_a_fresh_read(cache, clock):
    await _serve(cache, _Builder())
    clock.now += 30 + swr.SWR_MAX_STALE_SECONDS + 1
    build = _Builder()
    served = await _serve(cache, build)
    assert served.state == swr.MISS and build.calls == 1


async def test_an_invalidation_during_a_background_read_keeps_its_result_out(cache, clock):
    generation = {"token": "a"}
    await _serve(cache, _Builder(), generation=lambda: generation["token"])
    clock.now += 31
    build = _Builder(gated=True)
    await _serve(cache, build, generation=lambda: generation["token"])
    await asyncio.wait_for(build.entered.wait(), SAFETY_TIMEOUT)
    # The mutation: advance the token and sweep the key, as invalidate_instance_cache does.
    generation["token"] = "b"
    cache.delete(swr.NAMESPACE, _key())
    build.release.set()
    await _settle()
    assert _stored(cache) is None, "a read that an invalidation overtook must not be written back"
    after = _Builder()
    served = await _serve(cache, after, generation=lambda: generation["token"])
    assert served.state == swr.MISS and after.calls == 1


async def test_an_older_read_never_overwrites_a_newer_one_from_another_worker(cache, clock):
    """In one worker a forced read queues behind a background one (the lock). Across workers it
    does not, so a slow background read can finish after another worker's forced read: the
    write records when each build started, and the older one stands down."""
    newer_started = clock.now + 5
    cache.set(
        swr.NAMESPACE,
        _key(),
        wrap({"tag": "newer"}, stored_at=newer_started + 1, build_started_at=newer_started, fresh_for=30),
        ttl=210,
    )

    async def older() -> swr.Built:
        return swr.Built(value={"tag": "older"}, body={"tag": "older"}, fresh_for=30)

    # This build started at clock.now, before the other worker's.
    await swr._build_and_store(cache, _key(), BASE, older, None, "things")
    assert cache.get(swr.NAMESPACE, _key()) == {"tag": "newer"}


async def _forced_body() -> swr.Built:
    return swr.Built(value={"forced": True}, body={"forced": True}, fresh_for=30)


async def test_fresh_true_always_reads_and_concurrent_forced_calls_collapse(cache, clock):
    await _serve(cache, _Builder())
    build = _Builder(gated=True)
    first = asyncio.create_task(_serve(cache, build, fresh=True))
    await asyncio.wait_for(build.entered.wait(), SAFETY_TIMEOUT)
    second = asyncio.create_task(_serve(cache, build, fresh=True))
    await asyncio.sleep(0)
    build.release.set()
    a, b = await asyncio.wait_for(asyncio.gather(first, second), SAFETY_TIMEOUT)
    assert a.state == swr.BYPASS and build.calls == 1
    assert b.state == swr.HIT and b.body == {"tag": "a", "n": 1}


async def test_a_warm_read_never_queues_behind_a_rebuild(cache, clock):
    await _serve(cache, _Builder())
    build = _Builder(gated=True)
    forced = asyncio.create_task(_serve(cache, build, fresh=True))
    await asyncio.wait_for(build.entered.wait(), SAFETY_TIMEOUT)
    assert single_flight.is_rebuilding(_key())
    hit = await asyncio.wait_for(_serve(cache, _Builder()), 1)
    assert hit.state == swr.HIT
    clock.now += 31
    stale = await asyncio.wait_for(_serve(cache, _Builder()), 1)
    assert stale.state == swr.STALE
    assert swr._refreshing == {}, "no background read while a rebuild in this worker will write"
    build.release.set()
    await asyncio.wait_for(forced, SAFETY_TIMEOUT)


async def test_an_answer_the_handler_refuses_to_cache_leaves_the_old_entry(cache, clock):
    await _serve(cache, _Builder())
    clock.now += 31
    refused = _Builder(tag="b", fresh_for=None)
    served = await _serve(cache, refused)
    assert served.state == swr.STALE
    await _settle()
    assert refused.calls == 1
    assert cache.get(swr.NAMESPACE, _key()) == {"tag": "a", "n": 1}


async def test_a_failed_background_read_keeps_the_entry_and_leaks_no_task(cache, clock):
    await _serve(cache, _Builder())
    clock.now += 31

    async def boom() -> swr.Built:
        raise RuntimeError("cluster went away")

    served = await _serve(cache, boom)
    assert served.state == swr.STALE
    await _settle()
    assert swr._refreshing == {}
    assert cache.get(swr.NAMESPACE, _key()) == {"tag": "a", "n": 1}


async def test_the_background_read_runs_with_the_callers_scope(cache, clock):
    org_scope.set_allowed_clusters({"c1"})
    await _serve(cache, _Builder())
    clock.now += 31
    build = _Builder(tag="b")
    await _serve(cache, build)
    # The request ends and the worker moves on to another caller before the read runs.
    org_scope.set_allowed_clusters({"c2"})
    await _settle()
    assert build.scopes == [{"c1"}]
    org_scope.set_allowed_clusters({"c1"})
    assert cache.get(swr.NAMESPACE, _key()) == {"tag": "b", "n": 1}


async def test_a_build_whose_scope_moved_is_never_written(cache, clock):
    org_scope.set_allowed_clusters({"c1"})

    async def drifts() -> swr.Built:
        org_scope.set_allowed_clusters({"c1", "c2"})
        return swr.Built(value={"x": 1}, body={"x": 1}, fresh_for=30)

    served = await asyncio.create_task(_serve(cache, drifts), context=contextvars.copy_context())
    assert served.state == swr.MISS
    assert cache.get(swr.NAMESPACE, _key()) is None


async def test_a_scope_never_reads_another_scopes_entry(cache, clock):
    org_scope.set_allowed_clusters({"c1"})
    await _serve(cache, _Builder())
    org_scope.set_allowed_clusters({"c2"})
    other = _Builder()
    served = await _serve(cache, other)
    assert served.state == swr.MISS and other.calls == 1


async def test_a_legacy_plain_body_is_served_and_never_refreshed(cache, clock):
    cache.set(swr.NAMESPACE, _key(), {"old": True}, ttl=30)
    build = _Builder()
    served = await _serve(cache, build)
    assert served.state == swr.HIT and served.body == {"old": True} and served.age is None
    assert swr._refreshing == {} and build.calls == 0


async def test_a_stale_entry_from_the_redis_down_fallback_is_a_miss(cache, clock, monkeypatch):
    await _serve(cache, _Builder())
    clock.now += 31
    real = cache.get_entry
    monkeypatch.setattr(cache, "get_entry", lambda ns, key: (real(ns, key)[0], False))
    build = _Builder()
    served = await _serve(cache, build)
    assert served.state == swr.MISS and build.calls == 1


class _FakeRedis:
    """GET, SETEX, DELETE and the two lease scripts: one Redis shared by every worker."""

    def __init__(self) -> None:
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def setex(self, key, ttl, value):
        self.data[key] = value

    def delete(self, *keys):
        for key in keys:
            self.data.pop(key, None)

    def register_script(self, source):
        def run(keys, args):
            key, token = keys[0], args[0]
            if "NX" in source:
                if key not in self.data:
                    self.data[key] = token
                    return 1
                return 1 if self.data[key] == token else 0
            if self.data.get(key) == token:
                del self.data[key]
                return 1
            return 0

        return run

    def leases(self):
        return [k for k in self.data if k.startswith("orcastra:swr_lease:")]


async def test_one_background_read_per_key_across_workers(clock):
    redis = _FakeRedis()
    cache = CacheManager(redis_url=None)
    cache.redis_client = redis
    cache._redis_configured = True
    await _serve(cache, _Builder())
    clock.now += 31
    first = _Builder(tag="b", gated=True)
    await _serve(cache, first)
    await asyncio.wait_for(first.entered.wait(), SAFETY_TIMEOUT)
    assert len(redis.leases()) == 1
    # Another worker: its own task map, the same Redis.
    held = dict(swr._refreshing)
    swr._refreshing.clear()
    second = _Builder(tag="c")
    served = await _serve(cache, second)
    assert served.state == swr.STALE
    assert swr._refreshing == {} and second.calls == 0, "the lease must stop a second worker"
    swr._refreshing.update(held)
    first.release.set()
    await _settle()
    assert redis.leases() == [], "the lease is released once the read has written"
    assert cache.get(swr.NAMESPACE, _key()) == {"tag": "b", "n": 1}


async def test_a_failed_read_keeps_the_lease_so_retries_are_spaced(clock):
    redis = _FakeRedis()
    cache = CacheManager(redis_url=None)
    cache.redis_client = redis
    cache._redis_configured = True
    await _serve(cache, _Builder())
    clock.now += 31

    async def boom() -> swr.Built:
        raise RuntimeError("down")

    await _serve(cache, boom)
    await _settle()
    assert len(redis.leases()) == 1
    retry = _Builder()
    await _serve(cache, retry)
    assert retry.calls == 0


async def test_the_request_context_records_what_was_served(cache, clock):
    context = RequestContext()
    set_request_context(context)
    await _serve(cache, _Builder())
    assert context.extra["cache"] == swr.MISS
    context2 = RequestContext()
    set_request_context(context2)
    clock.now += 31

    async def nested() -> swr.Built:
        # A builder that reads another cached list through serve(), as /nodes reads availability.
        inner = await swr.serve(
            cache=cache, base_key="inner:v1", fresh=False, build=_Builder(tag="i"),
            label="inner",
        )
        return swr.Built(value=inner.body, body={"outer": True}, fresh_for=30)

    await _serve(cache, nested)
    assert context2.extra == {"cache": swr.STALE, "cache_age_s": 31.0}
    # The response has gone out; the context object lives on in the background read's copy.
    context2.extra.clear()
    await _settle()
    assert context2.extra == {}, "a background read must not write the finished request's context"


async def test_background_reads_are_capped_per_worker(cache, clock, monkeypatch):
    monkeypatch.setattr(swr, "MAX_BACKGROUND_REFRESHES", 1)
    await _serve(cache, _Builder())
    org_scope.set_allowed_clusters({"c9"})
    await _serve(cache, _Builder())
    clock.now += 31
    gated = _Builder(gated=True)
    org_scope.set_allowed_clusters(None)
    await _serve(cache, gated)
    await asyncio.wait_for(gated.entered.wait(), SAFETY_TIMEOUT)
    org_scope.set_allowed_clusters({"c9"})
    other = _Builder()
    served = await _serve(cache, other)
    assert served.state == swr.STALE and other.calls == 0
    gated.release.set()
    await _settle()


async def test_shutdown_cancels_reads_in_flight(cache, clock):
    await _serve(cache, _Builder())
    clock.now += 31
    gated = _Builder(gated=True)
    await _serve(cache, gated)
    await asyncio.wait_for(gated.entered.wait(), SAFETY_TIMEOUT)
    await swr.shutdown()
    assert swr._refreshing == {}


async def test_an_invalidation_landing_during_the_write_is_not_outlived(cache, clock):
    generation = {"token": "a"}
    real_set = cache.set

    def set_then_invalidate(namespace, key, value, ttl=None, **kwargs):
        real_set(namespace, key, value, ttl=ttl, **kwargs)
        if namespace == swr.NAMESPACE and key == _key():
            # The mutation lands between the pre-write check and the write completing.
            generation["token"] = "b"
            cache.delete(swr.NAMESPACE, _key())
            real_set(swr.NAMESPACE, _key(), value, ttl=ttl)  # the racing write wins the sweep

    cache.set = set_then_invalidate
    served = await _serve(cache, _Builder(), generation=lambda: generation["token"])
    assert served.state == swr.MISS
    assert cache.get(swr.NAMESPACE, _key()) is None, "a body written across an invalidation must go"


async def test_a_forced_refresh_queues_behind_a_background_read_instead_of_racing_it(cache, clock):
    await _serve(cache, _Builder())
    clock.now += 31
    background = _Builder(tag="bg", gated=True)
    await _serve(cache, background)
    await asyncio.wait_for(background.entered.wait(), SAFETY_TIMEOUT)

    order = []

    async def forced_build() -> swr.Built:
        order.append("forced-start")
        return swr.Built(value={"tag": "forced"}, body={"tag": "forced"}, fresh_for=30)

    forced = asyncio.create_task(_serve(cache, forced_build, fresh=True))
    for _ in range(5):
        await asyncio.sleep(0)
    assert order == [], "a forced read must not run beside a background read of the same list"
    background.release.set()
    served = await asyncio.wait_for(forced, SAFETY_TIMEOUT)
    assert order == ["forced-start"]
    assert served.state == swr.BYPASS, "the refresh still reads the clusters itself"
    await _settle()
    assert cache.get(swr.NAMESPACE, _key()) == {"tag": "forced"}
