"""OrcaHub's hardware totals answer from the cache and read the clusters side by side.

/integrations/regions is polled about every 2.4 minutes. Its totals read each cluster's status one
after the other on every miss of a plain 120 s cache, so most polls waited 2.4 s on dev (p95
10.4 s, max 14.6 s), and a read where no cluster answered cached zeros for 120 s. They are now
served through the stale-while-revalidate layer (owner decision 2026-11-04: 2 minutes fresh plus
the 3 minute stale window), read concurrently under a wall per cluster, remembered for less when
a cluster is missing, and not remembered at all when none answered.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from app.core import cache as cache_module
from app.core import swr
from app.routers import integrations

RESOURCES = SimpleNamespace(cpu_total=8, memory_total=16, storage_total=100, gpu_total=1, gpu_cards_total=1)


class _Service:
    def __init__(self, delay=0.0, down=()):
        self.delay = delay
        self.down = set(down)
        self.reads = []
        self.lock = threading.Lock()
        self.in_flight = 0
        self.peak = 0

    def check_node_status(self, cluster_id):
        with self.lock:
            self.reads.append(cluster_id)
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
        try:
            time.sleep(self.delay)
            if cluster_id in self.down:
                return SimpleNamespace(connected=False, resources=None)
            return SimpleNamespace(connected=True, resources=RESOURCES)
        finally:
            with self.lock:
                self.in_flight -= 1


def _stored(base_fragment: str):
    cache = cache_module.get_cache_manager()
    return [k for k in cache.memory_cache.cache if base_fragment in k]


@pytest.mark.asyncio
async def test_clusters_are_read_side_by_side():
    service = _Service(delay=0.3)
    started = time.monotonic()
    totals = await integrations._integration_hardware_totals(service, {"a", "b", "c"})
    elapsed = time.monotonic() - started
    assert totals == {"cpu": 24, "memory": 48, "storage": 300, "gpu": 3, "gpu_cards": 3}
    assert service.peak >= 2
    assert elapsed < 0.8, elapsed


@pytest.mark.asyncio
async def test_a_complete_read_is_served_from_the_cache_next_time():
    service = _Service()
    first = await integrations._integration_hardware_totals(service, {"a", "b"})
    second = await integrations._integration_hardware_totals(service, {"a", "b"})
    assert first == second
    assert sorted(service.reads) == ["a", "b"], "the second poll read the clusters again"
    [key] = _stored("integration_hardware")
    assert key.startswith("orcastra:api_responses:regions:integration_hardware:v1:") and ":scope=" in key


@pytest.mark.asyncio
async def test_no_answer_is_not_remembered():
    service = _Service(down={"a", "b"})
    assert await integrations._integration_hardware_totals(service, {"a", "b"}) == {
        "cpu": 0, "memory": 0, "storage": 0, "gpu": 0, "gpu_cards": 0
    }
    assert _stored("integration_hardware") == []
    await integrations._integration_hardware_totals(service, {"a", "b"})
    assert len(service.reads) == 4, "zeros were served from the cache"


@pytest.mark.asyncio
async def test_a_missing_cluster_is_remembered_briefly(monkeypatch):
    captured = {}
    real = swr._build_and_store

    async def spy(cache, key, base_key, build, generation, label):
        built = await real(cache, key, base_key, build, generation, label)
        captured["fresh_for"] = built.fresh_for
        return built

    monkeypatch.setattr(swr, "_build_and_store", spy)
    await integrations._integration_hardware_totals(_Service(down={"b"}), {"a", "b"})
    assert captured["fresh_for"] == integrations._TOTALS_DEGRADED_SECONDS
    await integrations._integration_hardware_totals(_Service(), {"x", "y"})
    assert captured["fresh_for"] == integrations._TOTALS_FRESH_SECONDS


@pytest.mark.asyncio
async def test_different_cluster_sets_never_share_an_answer():
    await integrations._integration_hardware_totals(_Service(), {"a"})
    await integrations._integration_hardware_totals(_Service(), {"a", "b"})
    assert len(_stored("integration_hardware")) == 2


@pytest.mark.asyncio
async def test_a_status_read_that_raises_counts_as_missing():
    class _Raising(_Service):
        def check_node_status(self, cluster_id):
            if cluster_id == "b":
                raise RuntimeError("boom")
            return super().check_node_status(cluster_id)

    totals = await integrations._integration_hardware_totals(_Raising(), {"a", "b"})
    assert totals["cpu"] == 8


@pytest.mark.asyncio
async def test_an_empty_grant_reads_nothing():
    service = _Service()
    assert await integrations._integration_hardware_totals(service, set()) == {
        "cpu": 0, "memory": 0, "storage": 0, "gpu": 0, "gpu_cards": 0
    }
    assert service.reads == []
