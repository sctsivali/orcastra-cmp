"""The collector's storage pool prewarm must not run pylxd on the leader's event loop.

It used to call the client directly from the prewarm coroutine, serially per cluster: a median
5.7 s and up to 13.9 s per run in which the leader worker answered nothing. It now goes through
the /storage/pools/resources-all handler, whose reads run in a thread pool.
"""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from app.core import cache as cache_module
from app.core.cache import CacheManager
from app.core import single_flight
from app.routers import storage_pools
from app.services.metrics_prewarm import CachePrewarm
from tests._loop_probes import (
    SAFETY_TIMEOUT,
    WorkerGate,
    assert_loop_runs_callbacks,
    wait_for_thread_event,
)


class _Pool:
    def __init__(self, gate: WorkerGate, name: str) -> None:
        self.name = name
        self.used_by = []
        self._gate = gate
        self.resources = SimpleNamespace(get=self._read)

    def _read(self):
        self._gate.hold()
        return SimpleNamespace(space={"used": 1, "total": 2}, inodes={})


class _Client:
    def __init__(self, gate: WorkerGate) -> None:
        self.storage_pools = SimpleNamespace(all=lambda: [_Pool(gate, "default")])


class _Service:
    def __init__(self, gate: WorkerGate) -> None:
        self._client = _Client(gate)
        self.connection_manager = SimpleNamespace(get_node_names=lambda: ["c1"])

    def get_client_with_reconnect(self, node_name):
        return self._client


class _Prewarm(CachePrewarm):
    def __init__(self, service) -> None:
        self._lxd_service = service


@pytest.fixture
def memory_cache(monkeypatch):
    manager = CacheManager(redis_url=None)
    monkeypatch.setattr(cache_module, "_global_cache", manager)
    monkeypatch.setattr(storage_pools, "get_cache_manager", lambda: manager)
    single_flight._locks.clear()
    single_flight._waiters.clear()
    return manager


async def test_prewarm_reads_pool_resources_off_the_loop(memory_cache):
    gate = WorkerGate(threading.get_ident())
    prewarm = _Prewarm(_Service(gate))

    task = asyncio.create_task(prewarm._prewarm_storage_pool_resources())
    try:
        await wait_for_thread_event(gate.entered, task=task, what="the pool resources read")
        assert gate.thread_id != gate.loop_thread, "the pylxd read ran on the event loop thread"
        await assert_loop_runs_callbacks()
        assert not task.done()
    finally:
        gate.release.set()
    await asyncio.wait_for(task, SAFETY_TIMEOUT)

    from app.core.org_scope import get_scope_cache_key

    cached = memory_cache.get("api_responses", get_scope_cache_key("storage_pool_resources:all:e1"))
    assert cached == {"resources": {"c1-default": {"space": {"used": 1, "total": 2}, "inodes": {}}}}


def test_prewarm_no_longer_writes_the_pool_resources_key_itself():
    import ast
    import inspect

    from app.services import metrics_prewarm

    tree = ast.parse(inspect.getsource(metrics_prewarm))
    literals = {
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "storage_pool_resources:all" not in literals, (
        "the prewarm must not write the endpoint's key; the handler owns it and its fence"
    )
