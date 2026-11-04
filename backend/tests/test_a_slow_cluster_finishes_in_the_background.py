"""A fleet list waits at most CLUSTER_WALL_SECONDS for any one cluster, and a slower one is not lost.

central-1's volume listing takes 17 to 24 s on dev, so the storage volume list's cold read took
as long. Owner decision 2026-11-04: answer within 15 s and name the cluster as still loading, then
show it once it answers. gather_clusters returns LateRead for a cluster past its wall and lets the
read run on; its result then stands in for that cluster in later answers of the same list and
scope, one background rebuild is scheduled so the rows appear without waiting for a poll to miss,
and a stand-in that ages is re-read in the background while it keeps answering.
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core import org_scope, swr
from app.services.lxd import fanout
from app.services.lxd.fanout import LateRead, NodeRead, collect, gather_clusters

POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="test_gather")


@pytest.fixture(autouse=True)
def _clean_memo():
    fanout._late.clear()
    yield
    fanout._late.clear()


class _Reader:
    def __init__(self, delays):
        self.delays = delays
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, node):
        with self.lock:
            self.calls.append(node)
        time.sleep(self.delays.get(node, 0))
        return NodeRead(rows=[f"{node}-row-{len(self.calls)}"])


async def _gather(reader, nodes=("fast", "slow"), wall=0.2, generation=None):
    return await gather_clusters(
        list(nodes), reader, executor=POOL, label="test.list", memo_key="test:list",
        generation=generation, wall=wall,
    )


def test_a_cluster_inside_its_wall_answers_as_before():
    reader = _Reader({"fast": 0.0, "slow": 0.0})
    results = asyncio.run(_gather(reader))
    assert all(isinstance(r, NodeRead) for r in results)


def test_a_cluster_past_its_wall_is_late_and_its_finished_read_stands_in():
    async def scenario():
        reader = _Reader({"fast": 0.0, "slow": 0.5})
        started = time.monotonic()
        first = await _gather(reader)
        waited = time.monotonic() - started
        assert isinstance(first[0], NodeRead) and isinstance(first[1], LateRead)
        assert waited < 0.45, "the answer waited for the slow cluster"
        await asyncio.sleep(0.5)
        second = await _gather(reader)
        assert isinstance(second[1], NodeRead), "the finished late read did not stand in"
        # fast was read twice, slow only once: the stand-in answered for it.
        assert sorted(reader.calls) == ["fast", "fast", "slow"]

    asyncio.run(scenario())


def test_a_second_request_waits_on_the_running_read_instead_of_starting_another():
    async def scenario():
        reader = _Reader({"slow": 0.5})
        first = await _gather(reader, nodes=("slow",), wall=0.1)
        assert isinstance(first[0], LateRead)
        second = await _gather(reader, nodes=("slow",), wall=1.0)
        assert isinstance(second[0], NodeRead)
        assert reader.calls == ["slow"]

    asyncio.run(scenario())


def test_the_finished_read_schedules_one_rebuild(monkeypatch):
    scheduled = []
    monkeypatch.setattr(swr, "capture_refresh", lambda: (lambda: scheduled.append(1)))

    async def scenario():
        reader = _Reader({"slow": 0.3})
        await _gather(reader, nodes=("slow",), wall=0.05)
        await asyncio.sleep(0.5)

    asyncio.run(scenario())
    assert scheduled == [1]


def test_a_mutation_discards_the_stand_in():
    async def scenario():
        token = {"v": 1}
        reader = _Reader({"slow": 0.2})
        await _gather(reader, nodes=("slow",), wall=0.05, generation=lambda: token["v"])
        await asyncio.sleep(0.35)
        token["v"] = 2
        reader.delays["slow"] = 0.0
        result = await _gather(reader, nodes=("slow",), wall=0.5, generation=lambda: token["v"])
        assert isinstance(result[0], NodeRead)
        assert reader.calls == ["slow", "slow"], "a pre-mutation stand-in answered"

    asyncio.run(scenario())


def test_a_stand_in_never_crosses_a_scope():
    async def read_as(allowed, reader, wall):
        token = org_scope._allowed_clusters_var.set(allowed)
        try:
            return await _gather(reader, nodes=("slow",), wall=wall)
        finally:
            org_scope._allowed_clusters_var.reset(token)

    async def scenario():
        reader = _Reader({"slow": 0.2})
        await read_as({"slow", "a"}, reader, 0.05)
        await asyncio.sleep(0.35)
        reader.delays["slow"] = 0.0
        other = await read_as({"slow", "b"}, reader, 0.5)
        assert isinstance(other[0], NodeRead)
        assert reader.calls == ["slow", "slow"], "another scope's stand-in answered"

    asyncio.run(scenario())


def test_an_old_stand_in_is_reread_in_the_background_while_it_answers(monkeypatch):
    async def scenario():
        reader = _Reader({"slow": 0.2})
        await _gather(reader, nodes=("slow",), wall=0.05)
        await asyncio.sleep(0.35)
        [entry] = fanout._late.values()
        entry.finished_at -= fanout.LATE_REREAD_AFTER_SECONDS + 1
        old = entry.value
        reader.delays["slow"] = 0.0
        answered = await _gather(reader, nodes=("slow",), wall=0.05)
        assert answered[0] is old, "the old stand-in did not keep answering"
        await asyncio.sleep(0.2)
        assert reader.calls == ["slow", "slow"]
        assert entry.value is not old, "the background re-read did not replace it"

    asyncio.run(scenario())


def test_a_late_read_that_fails_is_no_stand_in():
    async def scenario():
        calls = []

        def failing(node):
            calls.append(node)
            time.sleep(0.2)
            raise RuntimeError("listing failed")

        first = await gather_clusters(["slow"], failing, executor=POOL, label="t", memo_key="t", wall=0.05)
        assert isinstance(first[0], LateRead)
        await asyncio.sleep(0.35)
        assert fanout._late == {} or all(e.finished_at is None for e in fanout._late.values())
        second = await gather_clusters(["slow"], failing, executor=POOL, label="t", memo_key="t", wall=0.5)
        assert isinstance(second[0], RuntimeError)
        assert calls == ["slow", "slow"]

    asyncio.run(scenario())


def test_collect_names_a_late_cluster_without_calling_it_unreachable(monkeypatch):
    fan = collect(["fast", "slow"], [NodeRead(rows=[1]), LateRead("slow", 15)], noun="volumes")
    assert fan.late_nodes == ["slow"]
    assert fan.unreachable_nodes == []
    assert fan.node_errors["slow"].startswith("late: still reading after 15 s")
    assert fan.cacheable is False and fan.cacheable_despite_outages is True
    hidden = collect(["slow"], [LateRead("slow", 15)], noun="volumes", reasons_visible=False)
    assert hidden.late_nodes == ["slow"] and hidden.node_errors == {}
