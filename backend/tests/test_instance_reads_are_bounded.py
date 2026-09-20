"""Instance inventory and detail reads must never pin the API event loop."""

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from requests import exceptions as requests_exceptions

from app.core import org_scope
from app.core.blocking import BlockingPoolFull, BlockingTimeout
from app.routers import instances, vms
from app.services.lxd import health, instance as instance_module
from app.services.lxd.connection import LXDConnectionManager
from app.services.lxd.exceptions import LXDConnectionError, LXDLocalUnavailableError
from app.services.lxd.instance import InstanceManager


HOST = "shared-cluster"


def test_cached_client_lookup_keeps_scope_and_breaker_guards():
    connection = LXDConnectionManager.__new__(LXDConnectionManager)
    cached_client = object()
    connection._clients = {HOST: cached_client}

    token = org_scope._allowed_clusters_var.set({"another-cluster"})
    try:
        assert connection.get_cached_client(HOST) is None
    finally:
        org_scope._allowed_clusters_var.reset(token)

    health.clear(HOST)
    assert connection.get_cached_client(HOST) is cached_client
    health.record_failure(HOST, "offline")
    try:
        assert connection.get_cached_client(HOST) is None
    finally:
        health.clear(HOST)


async def _thread_offload(fn, *args, **_kwargs):
    """A minimal offload fake that does not depend on the sandbox's event-loop socketpair."""
    result = []
    failure = []

    def _work():
        try:
            result.append(fn(*args))
        except Exception as exc:  # pragma: no cover - asserted below when present
            failure.append(exc)

    worker = threading.Thread(target=_work)
    worker.start()
    while worker.is_alive():
        await asyncio.sleep(0.005)
    worker.join()
    if failure:
        raise failure[0]
    return result[0]


@pytest.mark.asyncio
async def test_detail_reconnect_lookup_and_projection_run_off_the_event_loop(monkeypatch):
    manager = InstanceManager.__new__(InstanceManager)
    loop_thread = threading.get_ident()
    worker_threads = []
    record = SimpleNamespace(project="default")
    projected = SimpleNamespace(project="default")

    def _record(value):
        worker_threads.append(threading.get_ident())
        return value

    def _connect(_host):
        _record(None)
        time.sleep(0.10)
        return object()

    monkeypatch.setattr(manager, "_get_client_with_reconnect", _connect)
    monkeypatch.setattr(manager, "_get_node_endpoint", lambda _host: _record("endpoint"))
    monkeypatch.setattr(
        instance_module,
        "find_instance_in_projects",
        lambda *_args, **_kwargs: _record((record, "default")),
    )
    monkeypatch.setattr(
        manager,
        "_instance_to_vm_info",
        lambda *_args, **_kwargs: _record(projected),
    )

    # core.blocking has its own executor tests. This fake isolates the contract here: the
    # complete reconnect/lookup/state projection is the callable InstanceManager offloads.
    monkeypatch.setattr(instance_module, "run_blocking", _thread_offload)

    task = asyncio.create_task(manager.get_vm("mine", HOST, "default"))
    heartbeats = 0
    for _ in range(5):
        await asyncio.sleep(0.01)
        heartbeats += 1

    assert heartbeats == 5
    assert not task.done(), "the blocking detail read ran on the event loop"
    assert await task is projected
    assert worker_threads
    assert all(thread != loop_thread for thread in worker_threads)


@pytest.mark.asyncio
async def test_detail_uses_the_dedicated_bounded_pool(monkeypatch):
    manager = InstanceManager.__new__(InstanceManager)
    seen = {}

    async def _bounded(fn, *args, **kwargs):
        seen.update(fn=fn, args=args, options=kwargs)
        return "detail"

    monkeypatch.setattr(instance_module, "run_blocking", _bounded)

    assert await manager.get_vm("mine", HOST, "default") == "detail"
    assert seen["fn"] == manager._get_vm_blocking
    assert seen["args"] == ("mine", HOST, "default", None)
    assert seen["options"]["timeout"] == 30.0
    assert seen["options"]["max_in_flight"] == 8
    assert seen["options"]["executor"] is instance_module._instance_detail_executor


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocking_error",
    [
        BlockingTimeout("instance.detail", 30.0),
        BlockingPoolFull("instance.detail", 8),
    ],
)
async def test_detail_timeout_and_saturation_become_connection_failures(
    monkeypatch,
    blocking_error,
):
    manager = InstanceManager.__new__(InstanceManager)

    async def _unavailable(*_args, **_kwargs):
        raise blocking_error

    monkeypatch.setattr(instance_module, "run_blocking", _unavailable)

    with pytest.raises(LXDConnectionError) as caught:
        await manager.get_vm("mine", HOST, "default")

    assert caught.value.__cause__ is blocking_error
    assert "temporarily unavailable" in str(caught.value)


def test_detail_socket_timeout_becomes_a_connection_failure(monkeypatch):
    manager = InstanceManager.__new__(InstanceManager)
    monkeypatch.setattr(manager, "_get_client_with_reconnect", lambda _host: object())
    monkeypatch.setattr(manager, "_get_node_endpoint", lambda _host: "endpoint")
    monkeypatch.setattr(
        instance_module,
        "find_instance_in_projects",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(requests_exceptions.ReadTimeout("upstream stopped answering")),
    )

    with pytest.raises(LXDConnectionError) as caught:
        manager._get_vm_blocking("mine", HOST, "default")

    assert isinstance(caught.value.__cause__, requests_exceptions.ReadTimeout)


@pytest.mark.asyncio
async def test_inventory_uses_bounded_workers_and_preserves_a_partial_cluster(monkeypatch):
    manager = InstanceManager.__new__(InstanceManager)
    manager.cert_manager = SimpleNamespace(list_clusters=lambda: ["fast", "slow"])
    manager.connection_manager = SimpleNamespace(get_node_names=lambda: [])
    calls = []

    monkeypatch.setattr(
        manager,
        "_fetch_inventory_node",
        lambda node: ([f"vm-{node}"], True, True),
    )

    async def _bounded(fn, *args, **kwargs):
        calls.append((kwargs["label"], kwargs["timeout"], kwargs["max_in_flight"]))
        if args == ("slow",):
            return ([], False, False)
        return fn(*args)

    monkeypatch.setattr(instance_module, "run_blocking", _bounded)
    org_scope.set_allowed_clusters({"fast", "slow"})
    try:
        rows, connected, incomplete = await manager.list_all_vms_with_failures()
    finally:
        org_scope.set_allowed_clusters(None)

    assert rows == ["vm-fast"]
    assert connected == ["fast"]
    assert incomplete == ["slow"]
    assert calls[0] == (
        "instance.inventory.registry",
        instance_module._INSTANCE_REGISTRY_TIMEOUT_SECONDS,
        instance_module._INSTANCE_INVENTORY_WORKERS,
    )
    assert ("instance.inventory[fast]", 30.0, 20) in calls
    assert ("instance.inventory[slow]", 30.0, 20) in calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result, expected",
    [
        ((["vm"], True, True), (["vm"], [HOST], [])),
        ((["vm"], True, False), (["vm"], [HOST], [HOST])),
        ((["ignored"], False, False), ([], [], [HOST])),
    ],
)
async def test_scoped_inventory_is_bounded_and_preserves_failure_detail(
    monkeypatch,
    result,
    expected,
):
    manager = InstanceManager.__new__(InstanceManager)
    seen = {}

    async def _bounded(fn, *args, **kwargs):
        seen.update(fn=fn, args=args, options=kwargs)
        return result

    monkeypatch.setattr(instance_module, "run_blocking", _bounded)

    actual = await manager.list_vms_in_project_with_failures(HOST, "qa")

    assert actual == expected
    assert seen["fn"] == manager._fetch_project_inventory_node
    assert seen["args"] == (HOST, "qa")
    assert seen["options"]["timeout"] == 30.0
    assert seen["options"]["max_in_flight"] == 20
    assert seen["options"]["executor"] is instance_module._instance_inventory_executor


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocking_error",
    [
        BlockingTimeout("instance.inventory[only/qa]", 30.0),
        BlockingPoolFull("instance.inventory[only/qa]", 20),
    ],
)
async def test_scoped_inventory_timeout_or_saturation_is_unavailable(
    monkeypatch,
    blocking_error,
):
    manager = InstanceManager.__new__(InstanceManager)

    async def _unavailable(*_args, **_kwargs):
        raise blocking_error

    monkeypatch.setattr(instance_module, "run_blocking", _unavailable)

    with pytest.raises(LXDConnectionError) as caught:
        await manager.list_vms_in_project_with_failures(HOST, "qa")

    assert caught.value.__cause__ is blocking_error


def test_scoped_inventory_does_not_start_a_vault_reconnect_after_cache_miss(monkeypatch):
    manager = InstanceManager.__new__(InstanceManager)
    get_cached_client = Mock(return_value=None)
    get_client = Mock(side_effect=AssertionError("scoped inventory must not validate or reconnect"))
    manager.connection_manager = SimpleNamespace(
        get_cached_client=get_cached_client,
        get_client=get_client,
    )
    get_cluster_info = Mock()
    get_cluster_certificate = Mock()
    manager.cert_manager = SimpleNamespace(
        get_cluster_info=get_cluster_info,
        get_cluster_certificate=get_cluster_certificate,
    )
    get_endpoint = Mock(return_value="endpoint")
    monkeypatch.setattr(manager, "_get_node_endpoint", get_endpoint)

    with pytest.raises(LXDLocalUnavailableError):
        manager._fetch_project_inventory_node(HOST, "qa")
    get_cached_client.assert_called_once_with(HOST)
    get_client.assert_not_called()
    get_endpoint.assert_not_called()
    get_cluster_info.assert_not_called()
    get_cluster_certificate.assert_not_called()


def test_scoped_inventory_preserves_a_remote_breaker_failure(monkeypatch):
    manager = InstanceManager.__new__(InstanceManager)
    manager.connection_manager = SimpleNamespace(get_cached_client=Mock(return_value=None))
    manager.cert_manager = None
    get_endpoint = Mock(return_value="endpoint")
    monkeypatch.setattr(manager, "_get_node_endpoint", get_endpoint)
    monkeypatch.setattr(instance_module.health, "is_tripped", lambda host: host == HOST)

    with pytest.raises(LXDConnectionError) as caught:
        manager._fetch_project_inventory_node(HOST, "qa")

    assert not isinstance(caught.value, LXDLocalUnavailableError)
    assert str(caught.value) == "Instance inventory is temporarily unavailable"
    get_endpoint.assert_not_called()


def test_scoped_inventory_reuses_the_cached_client_without_a_validation_probe(monkeypatch):
    manager = InstanceManager.__new__(InstanceManager)
    cached_client = object()
    get_cached_client = Mock(return_value=cached_client)
    get_client = Mock(side_effect=AssertionError("cached scoped reads need no validation probe"))
    manager.connection_manager = SimpleNamespace(
        get_cached_client=get_cached_client,
        get_client=get_client,
    )
    manager.cert_manager = None
    monkeypatch.setattr(manager, "_get_node_endpoint", lambda _host: "endpoint")
    scoped_read = Mock(return_value=(["vm"], True, True))
    monkeypatch.setattr(manager, "_fetch_vms_from_node_detailed", scoped_read)

    assert manager._fetch_project_inventory_node(HOST, "qa") == (["vm"], True, True)
    get_cached_client.assert_called_once_with(HOST)
    get_client.assert_not_called()
    scoped_read.assert_called_once_with(
        HOST,
        "endpoint",
        project="qa",
        allow_vault_reconnect=False,
        client_override=cached_client,
    )


@pytest.mark.asyncio
async def test_inventory_registry_read_keeps_the_event_loop_alive(monkeypatch):
    manager = InstanceManager.__new__(InstanceManager)

    def _slow_registry():
        time.sleep(0.10)
        return [HOST]

    manager.cert_manager = SimpleNamespace(list_clusters=_slow_registry)
    manager.connection_manager = SimpleNamespace(get_node_names=lambda: [])
    monkeypatch.setattr(
        manager,
        "_fetch_inventory_node",
        lambda _node: (["vm"], True, True),
    )
    monkeypatch.setattr(instance_module, "run_blocking", _thread_offload)

    org_scope.set_allowed_clusters({HOST})
    try:
        task = asyncio.create_task(manager.list_all_vms_with_failures())
        heartbeats = 0
        for _ in range(5):
            await asyncio.sleep(0.01)
            heartbeats += 1
        assert heartbeats == 5
        assert not task.done(), "the Vault registry read ran on the event loop"
        assert await task == (["vm"], [HOST], [])
    finally:
        org_scope.set_allowed_clusters(None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocking_error",
    [
        BlockingTimeout("instance.inventory[only]", 30.0),
        BlockingPoolFull("instance.inventory[only]", 20),
    ],
)
async def test_inventory_timeout_or_saturation_is_unavailable_even_with_partial_data(
    monkeypatch,
    blocking_error,
):
    manager = InstanceManager.__new__(InstanceManager)
    manager.cert_manager = SimpleNamespace(list_clusters=lambda: ["fast", HOST])
    manager.connection_manager = SimpleNamespace(get_node_names=lambda: [])

    async def _bounded(fn, *args, **kwargs):
        if kwargs["label"] == "instance.inventory.registry":
            return fn(*args)
        if args == ("fast",):
            return (["vm-fast"], True, True)
        raise blocking_error

    monkeypatch.setattr(instance_module, "run_blocking", _bounded)
    org_scope.set_allowed_clusters({"fast", HOST})
    try:
        with pytest.raises(LXDConnectionError) as caught:
            await manager.list_all_vms_with_failures()
    finally:
        org_scope.set_allowed_clusters(None)

    assert caught.value.__cause__ is blocking_error


@pytest.mark.parametrize(
    "handler,name_argument",
    [(instances.get_instance, "instance_name"), (vms.get_vm, "vm_name")],
)
def test_detail_capacity_failure_is_http_503(handler, name_argument):
    class _Unavailable:
        async def get_vm(self, *_args, **_kwargs):
            raise LXDConnectionError("Instance detail is temporarily unavailable")

    org_scope.set_allowed_clusters({HOST})
    visible = org_scope._visible_projects_var.set({HOST: {"default"}})
    try:
        with pytest.raises(HTTPException) as caught:
            asyncio.run(
                handler(
                    **{
                        name_argument: "mine",
                        "host": HOST,
                        "project": "default",
                        "lxd_service": _Unavailable(),
                    }
                )
            )
    finally:
        org_scope._visible_projects_var.reset(visible)
        org_scope.set_allowed_clusters(None)

    assert caught.value.status_code == 503


def test_specifications_capacity_failure_is_http_503(monkeypatch):
    class _Unavailable:
        async def list_all_vms_with_failures(self):
            raise LXDConnectionError("Instance inventory is temporarily unavailable")

    class _Cache:
        def get(self, *_args, **_kwargs):
            return None

        def instance_list_generation(self):
            return 0

    monkeypatch.setattr(instances, "get_cache_manager", lambda: _Cache())

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            instances.list_instance_specifications(
                lxd_service=_Unavailable(),
                fresh=True,
            )
        )

    assert caught.value.status_code == 503
