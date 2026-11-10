"""A cluster's members are read side by side and folded in their own order.

Serial member and pool reads dominate the capacity aggregation, so the reads overlap and the
fold keeps the members' order. A members list that fails is remembered for five minutes,
because a member that stops answering makes LXD wait out its own deadline on every check.
"""

import contextvars
import threading
import time
from types import SimpleNamespace

import pytest

from app.core.cache import CacheManager
from app.services.lxd import member_reads
from app.services.lxd.member_reads import (
    MEMBERS_FAILURE_SECONDS,
    MEMBERS_READ_TIMEOUT,
    members_read_failed,
    read_members,
    read_members_list,
)

_probe: contextvars.ContextVar = contextvars.ContextVar("probe", default="unset")


def _resources(member: str) -> dict:
    return {"cpu": {"total": 8}, "memory": {"total": 1000, "used": 400}, "member": member}


@pytest.fixture
def fake(monkeypatch):
    """Per member: (delay seconds, resources or an exception, pools or an exception)."""
    plan = {}
    seen = []

    def first_round(client, member):
        seen.append((member, _probe.get()))
        delay, resources, _ = plan[member]
        time.sleep(delay)
        if isinstance(resources, Exception):
            raise resources
        listing = plan[member][2]
        return resources, f"t-{member}", listing

    def pool_space(client, member, pool):
        if pool.startswith("bad"):
            raise RuntimeError("pool not answering")
        return {"total": 100, "used": len(pool)}

    monkeypatch.setattr(member_reads, "_first_round", first_round)
    monkeypatch.setattr(member_reads, "read_pool_space", pool_space)
    return plan, seen


def test_answers_come_back_in_member_order_whatever_finished_first(fake):
    plan, _ = fake
    plan.update(
        {
            "m1": (0.15, _resources("m1"), [{"name": "local", "driver": "zfs"}, {"name": "remote", "driver": "ceph"}]),
            "m2": (0.0, _resources("m2"), [{"name": "local", "driver": "zfs"}]),
            "m3": (0.05, _resources("m3"), ["/1.0/storage-pools/legacy"]),
        }
    )

    reads = read_members(SimpleNamespace(), "c", ["m1", "m2", "m3"])

    assert [read.name for read in reads] == ["m1", "m2", "m3"]
    assert [read.resources["member"] for read in reads] == ["m1", "m2", "m3"]
    assert [(p.member, p.name, p.driver) for p in reads[0].pools] == [("m1", "local", "zfs"), ("m1", "remote", "ceph")]
    assert [(p.name, p.driver) for p in reads[2].pools] == [("legacy", "unknown")]


def test_members_are_read_at_the_same_time(fake, monkeypatch):
    plan, _ = fake
    gate = threading.Barrier(3, timeout=5)

    def waits(member):
        return (0.0, _resources(member), [])

    plan.update({name: waits(name) for name in ("a", "b", "c")})
    original = member_reads._first_round

    def together(client, member):
        gate.wait()  # raises BrokenBarrierError if the reads ran one at a time
        return original(client, member)

    monkeypatch.setattr(member_reads, "_first_round", together)

    reads = read_members(SimpleNamespace(), "c", ["a", "b", "c"])

    assert all(read.error is None for read in reads)


def test_a_member_that_fails_is_reported_and_its_pools_are_not_read(fake):
    plan, _ = fake
    plan.update(
        {
            "up": (0.0, _resources("up"), [{"name": "local"}, {"name": "bad-ceph"}]),
            "down": (0.0, ConnectionError("member offline"), [{"name": "never"}]),
            "half": (0.0, _resources("half"), RuntimeError("pool list refused")),
        }
    )

    up, down, half = read_members(SimpleNamespace(), "c", ["up", "down", "half"])

    assert isinstance(down.error, ConnectionError) and down.pools == [] and down.degraded == []
    assert [p.name for p in up.pools] == ["local"] and up.degraded == ["storage:bad-ceph"]
    assert half.pools == [] and half.degraded == ["storage"]


def test_each_read_runs_in_the_callers_context(fake):
    plan, seen = fake
    plan.update({"a": (0.0, _resources("a"), []), "b": (0.0, _resources("b"), [])})
    _probe.set("org-7")

    read_members(SimpleNamespace(), "c", ["a", "b"])

    assert sorted(seen) == [("a", "org-7"), ("b", "org-7")]


class _Members:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def get(self, params=None, timeout=None):
        self.calls.append({"params": params, "timeout": timeout})
        if self.error is not None:
            raise self.error
        return SimpleNamespace(json=lambda: {"metadata": [{"server_name": "m1", "status": "Online"}]})


def _client(members):
    return SimpleNamespace(api=SimpleNamespace(cluster=SimpleNamespace(members=members)))


def test_the_members_list_is_read_with_a_bounded_timeout():
    members = _Members()

    body = read_members_list(CacheManager(redis_url=None), _client(members), "c")

    assert body["metadata"][0]["server_name"] == "m1"
    assert members.calls == [{"params": {"recursion": 1}, "timeout": MEMBERS_READ_TIMEOUT}]
    assert MEMBERS_READ_TIMEOUT[1] < 25, "shorter than LXD's own deadline for a silent member"


def test_a_failed_members_list_is_remembered_and_a_node_refresh_forgets_it():
    cache = CacheManager(redis_url=None)
    members = _Members(error=TimeoutError("context deadline exceeded"))

    assert read_members_list(cache, _client(members), "c") is None
    assert members_read_failed(cache, "c") and not members_read_failed(cache, "other")
    assert MEMBERS_FAILURE_SECONDS == 300

    cache.invalidate_node_cache("c", ())

    assert not members_read_failed(cache, "c")


def test_the_aggregation_skips_a_remembered_failure_without_asking(monkeypatch):
    from app.services.lxd import node_resources

    cache = CacheManager(redis_url=None)
    members = _Members(error=TimeoutError("context deadline exceeded"))
    client = SimpleNamespace(
        host_info={"environment": {"server_clustered": True}},
        api=SimpleNamespace(cluster=SimpleNamespace(members=members)),
    )
    monkeypatch.setattr("app.core.cache.get_cache_manager", lambda: cache)

    first = node_resources.aggregate_microcloud_resources(client, "c", {})
    second = node_resources.aggregate_microcloud_resources(client, "c", {})

    assert first is None and second is None
    assert len(members.calls) == 1, "the second check used the remembered failure"
