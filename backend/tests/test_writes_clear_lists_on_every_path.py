"""A write clears the lists it changed on every path, failures and timeouts included.

The browser half lives in the frontend checks; these pin the server half:

- proxy writes (OrcaHub, MCP) advance the generation the instance lists fence on, clear on
  success, and clear again when LXD settles a 202;
- a delete or power action that fails, times out or finds the instance already gone clears the
  list;
- a write abandoned by its caller's timeout clears when its thread finishes;
- a finished move or copy and an ambiguous job verdict clear the lists;
- related lists (IPAM rows for networks, pool rows for volumes, profiles for projects, images
  for clusters) are cleared with them;
- the collector's prewarm never writes a list read before an invalidation back over it.
"""

from __future__ import annotations

import ast
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

import pytest
from fastapi import HTTPException

from app.core.blocking import BlockingTimeout, run_blocking
from app.core.cache import CacheManager

ROUTERS = Path(__file__).resolve().parents[1] / "app" / "routers"


class _RecordingCache:
    """Stands in for get_cache_manager(): records every clearance by family."""

    def __init__(self) -> None:
        self.calls: List[tuple] = []

    def __getattr__(self, name: str):
        def record(*args: Any, **kwargs: Any) -> None:
            self.calls.append((name, args))

        return record

    def kinds(self) -> set:
        out = set()
        for name, args in self.calls:
            if name == "invalidate_resource_cache":
                out.update(args)
            else:
                out.add(name)
        return out


# --- proxy writes -------------------------------------------------------------------------


def test_an_external_write_advances_the_generation_the_instance_lists_fence_on() -> None:
    manager = CacheManager(redis_url=None)
    for key in (
        "instances:list:v3:scope=all",
        "vms:list:host=None:scope=all",
        "projects:list:all:scope=all",
        "images:list:all:project=*:scope=all",
        "ipam:list:scope=all",
    ):
        manager.set("api_responses", key, {"stale": True})
    before = manager.instance_list_generation()

    manager.invalidate_after_external_write()

    assert manager.instance_list_generation() != before, (
        "a fleet read already in flight compares this token before writing; the old proxy path "
        "advanced resource_list_generation:instances instead and the read wrote the old list back"
    )
    leftover = [key for key in manager.memory_cache.cache if ":list:" in key]
    assert leftover == [], leftover


def _function(tree: ast.AST, name: str) -> ast.AST:
    return next(
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


def _calls(node: ast.AST, attr: str) -> List[ast.Call]:
    return [
        n for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == attr
    ]


def test_proxy_routes_clear_through_the_external_write_helper() -> None:
    tree = ast.parse((ROUTERS / "integrations.py").read_text(encoding="utf-8"))
    for call in _calls(tree, "invalidate_resource_cache"):
        first = call.args[0] if call.args else None
        assert not (isinstance(first, ast.Constant) and first.value in {"instances", "vms"}), (
            f"integrations.py:{call.lineno} clears instance lists under the wrong generation; "
            "use invalidate_after_external_write or invalidate_instance_cache"
        )

    raw = _function(tree, "raw_proxy")
    assert _calls(raw, "invalidate_after_external_write"), "raw_proxy must clear on a write"
    assert any(
        isinstance(n, ast.Call) and getattr(n.func, "id", None) == "invalidate_when_settled"
        for n in ast.walk(raw)
    ), "a 202 from the raw proxy must clear again when LXD settles it"

    operation = _function(tree, "proxy_operation")
    handlers = [
        h for h in ast.walk(operation)
        if isinstance(h, ast.ExceptHandler)
        and isinstance(h.type, ast.Name) and h.type.id in {"Exception", "BlockingTimeout"}
    ]
    for kind in ("Exception", "BlockingTimeout"):
        assert any(
            h.type.id == kind and _calls(h, "invalidate_after_external_write") for h in handlers
        ), f"proxy_operation's {kind} handler must clear: the write may have landed"


def test_every_proxy_write_is_a_list_mutation() -> None:
    from app.services.lxd.proxy_operations import (
        GUEST_ACCESS_OPERATIONS,
        LIST_MUTATIONS,
        PROXY_OPERATION_LEVELS,
    )

    writes = {op for op, level in PROXY_OPERATION_LEVELS.items() if level != "read"}
    assert LIST_MUTATIONS == writes - set(GUEST_ACCESS_OPERATIONS)
    for expected in ("delete_instance", "create_instance", "stop_instance", "delete_snapshot",
                     "create_network", "delete_project"):
        assert expected in LIST_MUTATIONS
    assert "exec_instance" not in LIST_MUTATIONS


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"type": "async", "operation": "/1.0/operations/abc-123"}, "abc-123"),
        ({"operation": "/1.0/operations/abc-123?project=p1"}, "abc-123"),
        ({"metadata": {"id": "op-9"}}, "op-9"),
        ({"type": "sync", "metadata": {}}, None),
        ({"operation": ""}, None),
        (None, None),
        ([], None),
    ],
)
def test_the_operation_id_of_an_accepted_raw_write(body: Any, expected: Any) -> None:
    from app.services.lxd.raw_proxy_operations import lxd_operation_id

    assert lxd_operation_id(body) == expected


# --- the settle waiter --------------------------------------------------------------------


@pytest.fixture
def settle(monkeypatch):
    from app.services import settle_invalidation

    monkeypatch.setattr(settle_invalidation, "WAIT_SLICE_SECONDS", 1)
    monkeypatch.setattr(settle_invalidation, "MIN_POLL_SECONDS", 0)
    yield settle_invalidation
    settle_invalidation._waiters.clear()
    settle_invalidation._by_bucket.clear()


async def _drain(module) -> None:
    for _ in range(50):
        if not module._waiters:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("a settle waiter did not finish")


async def test_a_waiter_clears_once_lxd_settles_the_operation(settle, monkeypatch) -> None:
    states = iter([{"settled": False}, {"settled": True, "status": "Success"}])
    monkeypatch.setattr(settle, "wait_for_operation", lambda *a, **k: next(states))
    cleared = []

    assert settle.invalidate_when_settled(
        lambda: object(), "op-1", "p1", lambda: cleared.append(True), label="t"
    )
    await _drain(settle)
    assert cleared == [True]


async def test_an_unreadable_operation_still_clears(settle, monkeypatch) -> None:
    def gone(*_a, **_k):
        raise RuntimeError("404 not found")

    monkeypatch.setattr(settle, "wait_for_operation", gone)
    cleared = []
    assert settle.invalidate_when_settled(
        lambda: object(), "op-2", None, lambda: cleared.append(True), label="t"
    )
    await _drain(settle)
    assert cleared == [True]


async def test_no_client_still_clears(settle) -> None:
    cleared = []

    def no_client():
        raise ConnectionError("cluster down")

    assert settle.invalidate_when_settled(no_client, "op-3", None, lambda: cleared.append(1), label="t")
    await _drain(settle)
    assert cleared == [1]


async def test_a_waiter_is_never_started_without_an_operation_or_past_the_cap(settle, monkeypatch) -> None:
    cleared = []
    assert not settle.invalidate_when_settled(lambda: object(), None, None, lambda: cleared.append(1), label="t")
    monkeypatch.setattr(settle, "MAX_WAITERS", 0)
    assert not settle.invalidate_when_settled(lambda: object(), "op", None, lambda: cleared.append(1), label="t")
    assert cleared == []


async def test_shutdown_cancels_waiters(settle, monkeypatch) -> None:
    def slow(*_a, **_k):
        time.sleep(0.05)
        return {"settled": False}

    monkeypatch.setattr(settle, "wait_for_operation", slow)
    cleared = []
    settle.invalidate_when_settled(lambda: object(), "op-4", None, lambda: cleared.append(1), label="t")
    await asyncio.sleep(0.02)
    await settle.shutdown()
    assert settle._waiters == set()
    assert cleared == [1], "a cancelled waiter still clears on its way out"


# --- writes abandoned by a timeout --------------------------------------------------------


async def test_a_timed_out_call_reports_when_it_really_finishes() -> None:
    executor = ThreadPoolExecutor(max_workers=1)
    finished = []
    try:
        with pytest.raises(BlockingTimeout) as raised:
            await run_blocking(time.sleep, 0.3, timeout=0.05, label="slow", executor=executor)
        assert raised.value.pending is not None and not raised.value.pending.done()
        raised.value.when_finished(lambda: finished.append(True))
        assert finished == [], "the callback must wait for the thread, not the timeout"
        await asyncio.sleep(0.5)
        assert finished == [True]
    finally:
        executor.shutdown(wait=True)


def test_without_a_pending_call_the_follow_up_runs_at_once() -> None:
    ran = []
    BlockingTimeout("x", 1.0).when_finished(lambda: ran.append(True))
    assert ran == [True]


# --- instance actions ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("module", "handlers"),
    [
        ("vms.py", ("start_vm", "stop_vm", "restart_vm", "delete_vm")),
        ("instances.py", ("start_instance", "stop_instance", "restart_instance", "delete_instance")),
    ],
)
def test_instance_actions_clear_the_lists_on_every_outcome(module: str, handlers: tuple) -> None:
    tree = ast.parse((ROUTERS / module).read_text(encoding="utf-8"))
    for name in handlers:
        fn = _function(tree, name)
        finals = [
            stmt for node in ast.walk(fn) if isinstance(node, ast.Try) for stmt in node.finalbody
        ]
        assert any(
            isinstance(n, ast.Call) and getattr(n.func, "id", None) == "clear_instance_lists_after_failure"
            for stmt in finals for n in ast.walk(stmt)
        ), f"{module} {name} must clear the instance lists in a finally block"


async def test_a_delete_that_finds_the_instance_gone_clears_the_list(monkeypatch) -> None:
    from app.routers import vms
    from app.services.lxd.exceptions import LXDOperationError

    from app.core import cache as cache_module

    cache = _RecordingCache()
    monkeypatch.setattr(cache_module, "get_cache_manager", lambda: cache)
    monkeypatch.setattr(cache_module, "_last_uncertain_clear", 0.0)
    monkeypatch.setattr(vms, "verify_project_access", lambda host, project: project or "default")

    class _Gone:
        async def delete_vm(self, **_kwargs):
            raise LXDOperationError("Instance not found")

    with pytest.raises(HTTPException) as raised:
        await vms.delete_vm(
            vm_name="ghost", host="nuc2", force=True, project="default",
            lxd_service=_Gone(), tag_service=SimpleNamespace(), _=None, _rbac=None,
        )
    assert raised.value.status_code == 404
    assert ("invalidate_instance_cache", ()) in cache.calls, (
        "a 404 means the listed instance is already gone; the list must stop showing it"
    )


# --- related lists ------------------------------------------------------------------------


def test_network_writes_clear_the_ipam_rows(monkeypatch) -> None:
    from app.routers import networks

    cache = _RecordingCache()
    monkeypatch.setattr(networks, "get_cache_manager", lambda: cache)
    networks._invalidate_network_lists()
    assert {"networks", "ipam"} <= cache.kinds()


def test_volume_writes_clear_the_pool_rows(monkeypatch) -> None:
    from app.routers import storage_volumes

    cache = _RecordingCache()
    monkeypatch.setattr(storage_volumes, "get_cache_manager", lambda: cache)
    storage_volumes._invalidate_volume_lists()
    assert {"storage_volumes", "storage_pools", "storage_pool_resources"} <= cache.kinds()


def test_project_writes_clear_per_project_resources(monkeypatch) -> None:
    from app.routers import projects

    cache = _RecordingCache()
    monkeypatch.setattr(projects, "get_cache_manager", lambda: cache)
    projects._invalidate_project_caches()
    assert {"projects", "project_usage", "profiles", "networks", "images", "storage_volumes"} <= cache.kinds()


def test_a_cluster_answering_again_clears_its_images() -> None:
    manager = CacheManager(redis_url=None)
    manager.set("api_responses", "images:list:all:project=*:scope=all", {"stale": True})
    manager.invalidate_cluster_reachability("nuc2")
    assert not any(key for key in manager.memory_cache.cache if "images:list" in key)


# --- moves, copies and ambiguous verdicts -------------------------------------------------


def test_a_migration_step_delivers_its_outbox_at_once() -> None:
    """The cache clearance a move or copy commits is an outbox row, delivered right after the step
    instead of at the 15 s recovery sweep. Every path (activation, retention, project relocation,
    rollback) ends in run_migration_job."""
    worker = Path(__file__).resolve().parents[1] / "app" / "services" / "migrations" / "worker.py"
    fn = _function(ast.parse(worker.read_text(encoding="utf-8")), "run_migration_job")
    body = list(fn.body)
    step = next(i for i, stmt in enumerate(body) if isinstance(stmt, ast.Try))
    after = body[step + 1:]
    assert any(
        isinstance(n, ast.Call) and getattr(n.func, "id", None) == "drain_migration_audits"
        for stmt in after for n in ast.walk(stmt)
    ), "run_migration_job must deliver the outbox after the step, outside its lease"


async def test_an_unknown_create_verdict_clears_the_instance_lists(monkeypatch) -> None:
    from app.services.lxd import instance as instance_module

    cache = _RecordingCache()

    async def fake_set(*_a, **_k):
        return True

    monkeypatch.setattr(instance_module, "_set_create_job", fake_set)
    monkeypatch.setattr(instance_module, "get_cache_manager", lambda: cache)
    row = SimpleNamespace(job_id="j1", locked_by="w", operation_id=None)
    assert await instance_module._mark_create_unknown(row, operation_id=None, error="lost")
    assert ("invalidate_instance_cache", ()) in cache.calls


# --- the collector's prewarm --------------------------------------------------------------


class _PrewarmCache:
    def __init__(self) -> None:
        self.token = 0
        self.writes: list = []
        self.deletes: list = []

    def get(self, *_a):
        return None

    def set(self, namespace, key, value, ttl):
        self.writes.append(key)

    def delete(self, namespace, key):
        self.deletes.append(key)


async def test_a_prewarm_that_raced_an_invalidation_writes_nothing() -> None:
    from app.services.metrics_prewarm import CachePrewarm

    cache = _PrewarmCache()

    async def fetch():
        cache.token += 1  # a mutation lands while the fleet is being read
        return {"vms": ["old"]}

    await CachePrewarm()._prewarm_single_cache(
        cache, "vms:list:x", fetch, ttl=180, generation=lambda: cache.token
    )
    assert cache.writes == []


async def test_an_undisturbed_prewarm_still_writes() -> None:
    from app.services.metrics_prewarm import CachePrewarm

    cache = _PrewarmCache()

    async def fetch():
        return {"vms": ["new"]}

    await CachePrewarm()._prewarm_single_cache(
        cache, "vms:list:x", fetch, ttl=180, generation=lambda: cache.token
    )
    assert cache.writes == ["vms:list:x"]


def test_failures_clear_at_most_once_per_interval_and_a_fence_refusal_never(monkeypatch) -> None:
    from app.core import cache as cache_module

    cache = _RecordingCache()
    monkeypatch.setattr(cache_module, "get_cache_manager", lambda: cache)
    monkeypatch.setattr(cache_module, "_last_uncertain_clear", 0.0)
    clears = lambda: sum(1 for name, _ in cache.calls if name == "invalidate_instance_cache")

    cache_module.clear_instance_lists_after_failure(None)
    assert clears() == 0, "success clears in the route itself, right after LXD answers"

    cache_module.clear_instance_lists_after_failure(HTTPException(status_code=409, detail="fenced"))
    assert clears() == 0, "the migration fence refused before the cluster was asked"

    for _ in range(50):  # a caller repeating a cheap failing call
        cache_module.clear_instance_lists_after_failure(HTTPException(status_code=404, detail="gone"))
    assert clears() == 1, "failures are throttled to one clearance per interval"

    monkeypatch.setattr(cache_module, "_last_uncertain_clear", 0.0)
    cache_module.clear_instance_lists_after_failure(RuntimeError("lost connection"))
    assert clears() == 2


def test_a_successful_action_clears_right_after_lxd_answers() -> None:
    """The clearance runs before the route's follow-up work (tags, webhooks), so an
    event-triggered read that lands meanwhile does not get the pre-delete list."""
    for module, name, call in (
        ("vms.py", "delete_vm", "delete_vm"),
        ("instances.py", "delete_instance", "delete_vm"),
        ("vms.py", "start_vm", "start_vm"),
        ("instances.py", "stop_instance", "stop_vm"),
    ):
        source = ast.get_source_segment(
            (ROUTERS / module).read_text(encoding="utf-8"),
            _function(ast.parse((ROUTERS / module).read_text(encoding="utf-8")), name),
        )
        lxd_call = source.index(f"lxd_service.{call}(")
        clear = source.index("get_cache_manager().invalidate_instance_cache()")
        follow_up = min(
            (
                source.index(marker, lxd_call)
                for marker in ("tag_service.", "activity_logger.log_activity", "emit_resource_event")
                if marker in source[lxd_call:]
            ),
            default=len(source),
        )
        assert lxd_call < clear < follow_up, f"{module} {name} clears after its follow-up work"
