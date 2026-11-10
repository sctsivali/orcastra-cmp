"""'Create & Start' answers before the instance exists and starts it in the background.

The list cache is cleared when the create is accepted, while the instance is still Stopped. The
background task clears the list caches again once the start lands, so the row does not read
Stopped for a full cache TTL after the cluster has the instance Running.
"""

import asyncio
from types import SimpleNamespace

from app.services.lxd import instance as instance_module


class _CacheSpy:
    def __init__(self) -> None:
        self.invalidations = 0

    def invalidate_instance_cache(self) -> None:
        self.invalidations += 1


def _service() -> instance_module.InstanceManager:
    return instance_module.InstanceManager.__new__(instance_module.InstanceManager)


def _client(started: list, fail_start: bool = False):
    def start(wait: bool = True) -> None:
        if fail_start:
            raise RuntimeError("no root device")
        started.append(wait)
    # The start path reads `.name` to rebind the instance onto a client whose read timeout
    # outlives the start, and calls `sync` afterwards.
    instance = SimpleNamespace(name="web01", start=start, sync=lambda: None)
    return SimpleNamespace(
        operations=SimpleNamespace(wait_for_operation=lambda op: None),
        instances=SimpleNamespace(get=lambda name: instance),
    )


def test_the_list_cache_is_cleared_after_the_background_start(monkeypatch) -> None:
    cache = _CacheSpy()
    monkeypatch.setattr("app.core.cache.get_cache_manager", lambda: cache)
    started: list = []
    asyncio.run(_service()._finalize_and_start(_client(started), "web01", "op-1", "mc-lab", "UAT-Herald"))
    assert started == [True]
    assert cache.invalidations == 1, "the row must not read Stopped for a TTL after the start"


def test_a_failed_start_clears_nothing_and_still_reports(monkeypatch) -> None:
    cache = _CacheSpy()
    monkeypatch.setattr("app.core.cache.get_cache_manager", lambda: cache)
    logged: list = []
    monkeypatch.setattr(
        "app.services.activity_logger.get_activity_logger",
        lambda: SimpleNamespace(log_activity=lambda **kw: logged.append(kw)),
    )
    asyncio.run(_service()._finalize_and_start(_client([], fail_start=True), "web01", "op-1", "mc-lab", "UAT-Herald"))
    assert cache.invalidations == 0
    # The create is judged and recorded first (instance.create, success); the start failure is
    # its own record.
    autostarts = [r for r in logged if r["action"] == "instance.autostart"]
    assert autostarts and autostarts[0]["result"] == "failure"


def test_a_cache_fault_never_reaches_the_start(monkeypatch) -> None:
    def broken():
        raise RuntimeError("redis away")
    monkeypatch.setattr("app.core.cache.get_cache_manager", broken)
    started: list = []
    asyncio.run(_service()._finalize_and_start(_client(started), "web01", None, "mc-lab", "default"))
    assert started == [True]
