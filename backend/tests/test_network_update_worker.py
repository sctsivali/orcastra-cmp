"""Network updates retain admission until scoped daemon readback completes, off the loop."""

import asyncio
import copy
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pylxd.exceptions import NotFound

from app.core.blocking import BlockingPoolFull, BlockingTimeout
from app.routers import networks
from app.services.migration_fence import MigrationFenceConflict


class Response:
    def __init__(self, body, status_code=200):
        self.body, self.status_code = body, status_code

    def json(self):
        return self.body


def update_fixture(monkeypatch, *, owner="default", network_type="bridge"):
    state = SimpleNamespace(
        lock=False, events=[], calls=[], puts=[], waits=[], invalidations=[],
        metadata={"name": "qa-network", "type": network_type, "managed": True, "status": "Created", "project": owner,
                  "description": "original", "config": {"ipv4.address": "10.55.0.1/24"}},
        readback=None, missing=False, apply=True, final="Success", block=None,
    )

    def record(name, **values):
        assert state.lock, f"{name} escaped network configuration admission"
        state.events.append(name)
        state.calls.append({"name": name, "thread": threading.get_ident(), **values})

    @asynccontextmanager
    async def lock(_db, _host):
        state.lock = True
        state.events.append("lock")
        try:
            yield
        finally:
            state.events.append("unlock")
            state.lock = False

    async def cluster_fence(**kwargs):
        record("cluster-fence", **kwargs)

    async def project_fence(**kwargs):
        record("project-fence", **kwargs)

    def wait_get(params=None):
        record("wait", params=params)
        state.waits.append(params)
        if state.apply and state.final == "Success":
            state.metadata.update(copy.deepcopy(state.puts[-1]["json"]))
        return Response({"type": "sync", "metadata": {
            "id": "op-update", "status": state.final, "err": "refused" if state.final == "Failure" else "",
        }})

    def get(params=None):
        record("get", params=params)
        if state.puts:
            assert state.waits, "Readback preceded operation completion"
            if state.missing:
                raise NotFound(Response({"type": "error", "error": "Network not found"}, 404))
            if state.readback is not None:
                return Response({"metadata": copy.deepcopy(state.readback)})
        return Response({"metadata": copy.deepcopy(state.metadata)})

    def put(*, json, params, timeout):
        record("put", params=params, timeout=timeout)
        state.puts.append({"json": copy.deepcopy(json), "params": params, "timeout": timeout})
        if state.block is not None:
            entered, release = state.block
            entered.set()
            assert release.wait(2), "Test failed to release the synthetic daemon"
        return Response({"type": "async", "operation": "/1.0/operations/op-update",
                         "metadata": {"id": "op-update"}}, 202)

    endpoint = SimpleNamespace(get=get, put=put)

    class NetworkCollection:
        def __getitem__(self, _name):
            return endpoint

    class OperationCollection:
        def __getitem__(self, _name):
            return SimpleNamespace(wait=SimpleNamespace(get=wait_get))

    client = SimpleNamespace(
        host_info={"api_extensions": ["network_acl", "network_bridge_acl"]},
        api=SimpleNamespace(networks=NetworkCollection(), operations=OperationCollection()),
    )

    def get_client(host):
        record("client", host=host)
        return client

    def resolve_owner(_client, kind, project):
        record("owner", kind=kind, project=project)
        return owner

    def list_acls(_client, collection, project):
        record("acl", collection=collection, project=project)
        return [{"name": "qa-allow"}]

    def invalidate(resource):
        record("invalidate", resource=resource)
        state.invalidations.append(resource)

    monkeypatch.setattr(networks, "serialize_cluster_configuration", lock)
    monkeypatch.setattr(networks, "assert_cluster_not_migration_fenced", cluster_fence)
    monkeypatch.setattr(networks, "assert_project_not_migration_fenced", project_fence)
    monkeypatch.setattr(networks, "verify_cluster_access", lambda host: None)
    monkeypatch.setattr(networks, "project_is_visible", lambda host, project: True)
    monkeypatch.setattr(networks, "resolve_owner", resolve_owner)
    monkeypatch.setattr(networks, "list_in_project", list_acls)
    monkeypatch.setattr(networks, "get_cache_manager", lambda: SimpleNamespace(invalidate_resource_cache=invalidate))
    return state, SimpleNamespace(get_client_with_reconnect=get_client)


async def update(service, *, config=None, description="updated"):
    return await networks.update_network(
        "qa-network", host="qa-own", project="tenant-a", config=config, description=description,
        lxd_service=service, db=None, _rbac=None,
    )


def test_update_keeps_the_loop_responsive_and_all_daemon_work_in_bounded_workers(monkeypatch):
    state, service = update_fixture(monkeypatch)
    state.block = entered, release = threading.Event(), threading.Event()
    admitted = []
    actual_run = networks.run_blocking

    async def tracked_run(function, *args, **kwargs):
        admitted.append(kwargs)
        return await actual_run(function, *args, **kwargs)

    monkeypatch.setattr(networks, "run_blocking", tracked_run)

    async def scenario():
        loop_thread = threading.get_ident()
        task = asyncio.create_task(update(service, config={"bridge.mtu": "1500", "security.acls": "qa-allow"}))
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.005)
            assert entered.is_set(), "Update never reached the blocked daemon"
            assert not task.done()
            assert state.lock
            heartbeat = asyncio.Event()
            asyncio.get_running_loop().call_soon(heartbeat.set)
            await asyncio.wait_for(heartbeat.wait(), 0.2)
            assert not release.is_set()
        finally:
            release.set()
            result = await asyncio.wait_for(task, 2)
        return loop_thread, result

    loop_thread, result = asyncio.run(scenario())
    assert result["success"] is True
    assert state.events == ["lock", "cluster-fence", "client", "owner", "project-fence",
                            "project-fence", "get", "acl", "put", "wait", "get", "invalidate",
                            "invalidate", "unlock"]
    assert len(admitted) == 2
    for admission in admitted:
        assert admission["timeout"] == 90
        assert admission["max_in_flight"] == 10
        assert admission["concurrency_key"] == "qa-own"
        assert admission["max_in_flight_per_key"] == 1
    assert all(call["thread"] != loop_thread for call in state.calls if call["name"] not in {"cluster-fence", "project-fence"})
    assert [call["project"] for call in state.calls if call["name"] == "project-fence"] == ["default", "tenant-a"]
    assert next(call for call in state.calls if call["name"] == "acl")["project"] == "default"
    assert all(call["params"] == {"project": "default"} for call in state.calls if call["name"] in {"get", "put"})
    assert state.puts[0]["timeout"] == (5, 60)
    assert state.waits == [{"timeout": 5, "project": "default"}]
    assert set(state.invalidations) == {"networks", "ipam"}


@pytest.mark.parametrize("fence", ["cluster", "project"])
def test_migration_fences_deny_before_a_daemon_mutation(monkeypatch, fence):
    state, service = update_fixture(monkeypatch)

    async def conflict(**kwargs):
        assert state.lock
        raise MigrationFenceConflict("synthetic migration fence")

    monkeypatch.setattr(networks, f"assert_{fence}_not_migration_fenced", conflict)
    with pytest.raises(HTTPException) as error:
        asyncio.run(update(service))
    assert error.value.status_code == 409
    assert state.puts == [] and state.waits == [] and state.invalidations == []
    assert not state.lock
    if fence == "cluster":
        assert not any(call["name"] == "client" for call in state.calls)


@pytest.mark.parametrize("failure", ["operation", "missing", "unchanged", "pending", "wrong-project"])
def test_failed_operation_or_unconfirmed_changes_are_never_success(monkeypatch, failure):
    state, service = update_fixture(monkeypatch)
    if failure == "operation":
        state.final = "Failure"
    elif failure == "missing":
        state.missing = True
    elif failure == "unchanged":
        state.apply = False
    else:
        state.readback = copy.deepcopy(state.metadata)
        state.readback["status" if failure == "pending" else "project"] = "Pending" if failure == "pending" else "qa-neighbor"
    with pytest.raises(HTTPException) as error:
        asyncio.run(update(service))
    assert error.value.status_code == (404 if failure == "missing" else 400 if failure == "operation" else 502)
    assert len(state.puts) == 1
    assert state.invalidations == []
    assert not state.lock


@pytest.mark.parametrize(("phase", "error", "status"), [
    (1, BlockingTimeout("owner", 0.1), 504),
    (2, BlockingTimeout("update", 0.1), 504),
    (1, BlockingPoolFull("owner", 1), 503),
    (2, BlockingPoolFull("update", 1), 503),
])
def test_worker_timeout_and_saturation_are_explicit(monkeypatch, phase, error, status):
    state, service = update_fixture(monkeypatch)
    calls = 0

    async def unavailable(function, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == phase:
            raise error
        return function(*args)

    monkeypatch.setattr(networks, "_run_network_blocking", unavailable)
    with pytest.raises(HTTPException) as response:
        asyncio.run(update(service))
    assert response.value.status_code == status
    assert state.puts == [] and state.invalidations == []
    assert not state.lock
    if status == 504:
        assert ("no update was submitted" if phase == 1 else "may be partially applied") in response.value.detail


def test_timeout_does_not_release_the_lock_until_the_admitted_worker_finishes(monkeypatch):
    state, service = update_fixture(monkeypatch)
    state.block = entered, release = threading.Event(), threading.Event()
    actual_run = networks.run_blocking

    async def short_update_deadline(function, *args, **kwargs):
        if kwargs["label"].startswith("update_network["):
            kwargs["timeout"] = 0.05
        return await actual_run(function, *args, **kwargs)

    monkeypatch.setattr(networks, "run_blocking", short_update_deadline)

    async def scenario():
        task = asyncio.create_task(update(service))
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.005)
            assert entered.is_set()
            await asyncio.sleep(0.1)
            assert state.lock
            assert not task.done(), "Timeout released migration admission while the write was still active"
        finally:
            release.set()
            with pytest.raises(HTTPException) as error:
                await asyncio.wait_for(task, 2)
        return error.value

    error = asyncio.run(scenario())
    assert error.status_code == 504
    assert "may be partially applied" in error.detail
    assert state.waits and set(state.invalidations) == {"networks", "ipam"}
    assert state.events[-1] == "unlock"
    assert not state.lock


def test_project_owned_ovn_operation_is_waited_on_in_its_resolved_scope(monkeypatch):
    state, service = update_fixture(monkeypatch, owner="tenant-a", network_type="ovn")
    result = asyncio.run(update(service))
    assert result["success"] is True
    assert state.waits == [{"timeout": 5, "project": "tenant-a"}]
    assert all(call["params"] == {"project": "tenant-a"} for call in state.calls if call["name"] in {"get", "put"})


def test_denied_resolved_owner_never_reads_or_changes_its_networks(monkeypatch):
    state, service = update_fixture(monkeypatch)
    monkeypatch.setattr(networks, "project_is_visible", lambda host, project: project == "tenant-a")
    with pytest.raises(HTTPException) as error:
        asyncio.run(update(service))
    assert error.value.status_code == 403
    assert not any(call["name"] in {"get", "acl", "put", "wait"} for call in state.calls)
    assert not state.lock


def test_cancellation_drains_the_worker_before_releasing_migration_admission(monkeypatch):
    state, service = update_fixture(monkeypatch)
    state.block = entered, release = threading.Event(), threading.Event()

    async def scenario():
        task = asyncio.create_task(update(service))
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.005)
            assert entered.is_set()
            task.cancel()
            await asyncio.sleep(0.05)
            assert state.lock and not task.done()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())
    assert state.waits and set(state.invalidations) == {"networks", "ipam"}
    assert state.events[-1] == "unlock"
    assert not state.lock
