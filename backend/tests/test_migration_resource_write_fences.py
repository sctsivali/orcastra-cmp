"""Migration admission and ordinary infrastructure writes share one cluster boundary."""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.blocking import BlockingTimeout
from app.routers import lxd_server, network_acls, networks, profiles, projects
from app.routers import storage_pools, storage_volumes
from app.services import project_deletion


WRITE_HANDLERS = (
    networks.create_network,
    networks.update_network,
    networks.delete_network,
    network_acls.create_network_acl,
    network_acls.update_network_acl,
    network_acls.delete_network_acl,
    storage_pools.create_storage_pool,
    storage_pools.update_storage_pool,
    storage_pools.delete_storage_pool,
    storage_volumes.create_storage_volume,
    storage_volumes.update_storage_volume,
    storage_volumes.delete_storage_volume,
    profiles.create_profile,
    profiles.update_profile,
    profiles.delete_profile,
    projects.create_project,
    projects.update_project,
    # The delete route and the integration proxy share this one; the route passes it the request
    # db (test below).
    project_deletion.delete_project_and_forget,
    lxd_server.update_server_config,
)


def _call_names(function: Any) -> list[tuple[str, ast.Call]]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    calls: list[tuple[str, ast.Call]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Name):
            calls.append((target.id, node))
        elif isinstance(target, ast.Attribute):
            calls.append((target.attr, node))
    return calls


@pytest.mark.parametrize("handler", WRITE_HANDLERS, ids=lambda fn: fn.__name__)
def test_every_migration_sensitive_write_uses_the_request_db_and_cluster_serializer(
    handler: Any,
) -> None:
    assert "db" in inspect.signature(handler).parameters
    calls = _call_names(handler)
    serializer_calls = [call for name, call in calls if name == "serialize_cluster_configuration"]
    assert len(serializer_calls) == 1
    assert isinstance(serializer_calls[0].args[0], ast.Name)
    assert serializer_calls[0].args[0].id == "db"

    fence_calls = [call for name, call in calls if name.startswith("assert_") and "migration_fenced" in name]
    assert fence_calls
    for call in fence_calls:
        db_keywords = [keyword for keyword in call.keywords if keyword.arg == "db"]
        assert len(db_keywords) == 1
        assert isinstance(db_keywords[0].value, ast.Name)
        assert db_keywords[0].value.id == "db"


def test_the_project_delete_route_hands_its_request_db_to_the_shared_delete() -> None:
    assert "db" in inspect.signature(projects.delete_project).parameters
    delegations = [
        call for name, call in _call_names(projects.delete_project) if name == "delete_project_and_forget"
    ]
    assert len(delegations) == 1
    assert isinstance(delegations[0].args[0], ast.Name) and delegations[0].args[0].id == "db"


@pytest.mark.asyncio
async def test_network_timeout_drains_the_admitted_worker_before_releasing_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}
    worker_states = iter((1, 0))

    async def timed_out(*_args: Any, **kwargs: Any) -> None:
        observed.update(kwargs)
        raise BlockingTimeout("network.create", 1)

    def in_flight(executor: Any, host: str) -> int:
        assert executor is networks._executor
        assert host == "cluster-a"
        return next(worker_states)

    monkeypatch.setattr(networks, "run_blocking", timed_out)
    monkeypatch.setattr(networks, "in_flight_for_key", in_flight)

    with pytest.raises(BlockingTimeout):
        await networks._run_network_blocking(
            lambda: None,
            host="cluster-a",
            label="network.create[cluster-a/test]",
        )

    assert observed["concurrency_key"] == "cluster-a"
    assert observed["max_in_flight_per_key"] == networks.NETWORK_MAX_IN_FLIGHT_PER_CLUSTER


@pytest.mark.asyncio
async def test_project_delete_blocks_concurrent_migration_admission_until_daemon_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon_started = asyncio.Event()
    release_daemon = asyncio.Event()
    migration_entered = asyncio.Event()
    cluster_lock = asyncio.Lock()
    db = object()

    @asynccontextmanager
    async def configuration_lock(observed_db: Any, host: str, *, wait_seconds: Any = None):
        assert observed_db is db
        assert host == "cluster-a"
        async with cluster_lock:
            yield

    class ProjectService:
        async def delete_project(self, host: str, project: str) -> None:
            assert cluster_lock.locked()
            assert (host, project) == ("cluster-a", "source")
            daemon_started.set()
            await release_daemon.wait()

    async def fence(**kwargs: Any) -> None:
        assert cluster_lock.locked()
        assert kwargs == {
            "cluster_id": "cluster-a",
            "project": "source",
            "db": db,
        }

    async def forget(*_args: Any, **_kwargs: Any) -> str:
        assert cluster_lock.locked()
        return ""

    async def no_event(**_kwargs: Any) -> None:
        return None

    from app.services.webhooks import emitter

    async def no_rules(*_args: Any, **_kwargs: Any) -> frozenset:
        assert cluster_lock.locked()
        return frozenset()

    monkeypatch.setattr(project_deletion, "serialize_cluster_configuration", configuration_lock)
    monkeypatch.setattr(project_deletion, "assert_project_not_migration_fenced", fence)
    monkeypatch.setattr(projects, "verify_cluster_access", lambda _host: None)
    monkeypatch.setattr(projects, "_refuse_a_project_outside_the_bound", lambda *_args: None)
    monkeypatch.setattr(project_deletion.project_grants, "forget_deleted_project", forget)
    monkeypatch.setattr(project_deletion.project_grants, "rule_ids_naming", no_rules)
    monkeypatch.setattr(projects, "_invalidate_project_caches", lambda: None)
    monkeypatch.setattr(projects.activity_logger, "log_activity", lambda **_kwargs: None)
    monkeypatch.setattr(projects, "schedule_orcahub_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(emitter, "emit_resource_event", no_event)

    deletion = asyncio.create_task(
        projects.delete_project(
            host="cluster-a",
            project_name="source",
            lxd_service=ProjectService(),
            _=None,
            _rbac=object(),
            db=db,
        )
    )
    await asyncio.wait_for(daemon_started.wait(), timeout=1)

    async def admit_migration() -> None:
        async with configuration_lock(db, "cluster-a"):
            migration_entered.set()

    admission = asyncio.create_task(admit_migration())
    await asyncio.sleep(0)
    assert not migration_entered.is_set()

    release_daemon.set()
    result = await asyncio.wait_for(deletion, timeout=1)
    await asyncio.wait_for(admission, timeout=1)
    assert result["success"] is True
    assert migration_entered.is_set()


class _Response:
    def __init__(self, metadata: dict[str, Any] | None = None, status_code: int = 200) -> None:
        self.status_code = status_code
        self._metadata = metadata or {}

    def json(self) -> dict[str, Any]:
        return {"metadata": self._metadata}


@pytest.mark.asyncio
async def test_acl_delete_checks_the_effective_inherited_owner_inside_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"held": False}
    events: list[str] = []
    db = object()

    @asynccontextmanager
    async def configuration_lock(observed_db: Any, host: str):
        assert (observed_db, host) == (db, "cluster-a")
        state["held"] = True
        events.append("lock")
        try:
            yield
        finally:
            state["held"] = False

    class ACL:
        def delete(self, **kwargs: Any) -> _Response:
            assert state["held"]
            assert kwargs["params"] == {"project": "default"}
            events.append("delete")
            return _Response(status_code=202)

    class ACLs:
        def __getitem__(self, _name: str) -> ACL:
            return ACL()

    class API:
        def __getitem__(self, key: str) -> ACLs:
            assert key == "network-acls"
            return ACLs()

    client = SimpleNamespace(api=API())

    async def get_client(*_args: Any, **_kwargs: Any) -> Any:
        assert state["held"]
        return client

    def owner(*_args: Any) -> str:
        assert state["held"]
        events.append("owner")
        return "default"

    async def fence(**kwargs: Any) -> None:
        assert state["held"]
        assert kwargs["cluster_id"] == "cluster-a"
        assert kwargs["db"] is db
        events.append(f"fence:{kwargs['project']}")

    def wait(*_args: Any, **_kwargs: Any) -> None:
        assert state["held"]
        events.append("wait")

    monkeypatch.setattr(network_acls, "serialize_cluster_configuration", configuration_lock)
    monkeypatch.setattr(network_acls, "client_for", get_client)
    monkeypatch.setattr(network_acls, "require_client", lambda value, _host: value)
    monkeypatch.setattr(network_acls, "resolve_owner", owner)
    monkeypatch.setattr(network_acls, "project_is_visible", lambda *_args: True)
    monkeypatch.setattr(network_acls, "verify_cluster_access", lambda _host: None)
    monkeypatch.setattr(network_acls, "assert_project_not_migration_fenced", fence)
    monkeypatch.setattr(network_acls, "wait_for_lxd_operation", wait)
    monkeypatch.setattr(network_acls.activity_logger, "log_activity", lambda **_kwargs: None)

    result = await network_acls.delete_network_acl(
        acl_name="allow-web",
        host="cluster-a",
        project="tenant-a",
        lxd_service=object(),
        _=None,
        _rbac=object(),
        db=db,
    )

    assert result["success"] is True
    assert events == [
        "lock",
        "owner",
        "fence:default",
        "fence:tenant-a",
        "delete",
        "wait",
    ]


@pytest.mark.asyncio
async def test_volume_update_addresses_and_fences_the_effective_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"held": False}
    seen: dict[str, Any] = {}
    db = object()

    @asynccontextmanager
    async def configuration_lock(_db: Any, _host: str):
        state["held"] = True
        try:
            yield
        finally:
            state["held"] = False

    class VolumeNode:
        def get(self, *, params: dict[str, str]) -> _Response:
            assert state["held"]
            seen["get_params"] = params
            return _Response({"config": {}, "description": "before"})

        def put(self, *, json: dict[str, Any], params: dict[str, str]) -> None:
            assert state["held"]
            seen["put"] = (json, params)

    async def get_client(*_args: Any, **_kwargs: Any) -> object:
        return object()

    async def fence(**kwargs: Any) -> None:
        assert state["held"]
        seen.setdefault("fences", []).append(kwargs)

    monkeypatch.setattr(storage_volumes, "serialize_cluster_configuration", configuration_lock)
    monkeypatch.setattr(storage_volumes, "client_for", get_client)
    monkeypatch.setattr(storage_volumes, "require_client", lambda value, _host: value)
    monkeypatch.setattr(storage_volumes, "resolve_owner", lambda *_args: "default")
    monkeypatch.setattr(storage_volumes, "project_is_visible", lambda *_args: True)
    monkeypatch.setattr(storage_volumes, "verify_cluster_access", lambda _host: None)
    monkeypatch.setattr(storage_volumes, "assert_project_not_migration_fenced", fence)
    monkeypatch.setattr(storage_volumes, "_volume_node", lambda *_args: VolumeNode())
    monkeypatch.setattr(
        storage_volumes,
        "get_cache_manager",
        lambda: SimpleNamespace(invalidate_resource_cache=lambda *_args: None),
    )

    result = await storage_volumes.update_storage_volume(
        volume_type="custom",
        volume_name="data",
        host="cluster-a",
        pool="pool-a",
        config=None,
        description="after",
        project="tenant-a",
        location="member-a",
        lxd_service=object(),
        _=None,
        _rbac=object(),
        db=db,
    )

    assert result["success"] is True
    assert seen["fences"] == [
        {"cluster_id": "cluster-a", "project": "default", "db": db},
        {"cluster_id": "cluster-a", "project": "tenant-a", "db": db},
    ]
    assert seen["get_params"] == {"target": "member-a"}
    assert seen["put"][1] == {"target": "member-a"}


@pytest.mark.asyncio
async def test_server_config_waits_for_completion_under_the_cluster_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"held": False}
    events: list[str] = []
    db = object()
    response = _Response(status_code=202)

    @asynccontextmanager
    async def configuration_lock(_db: Any, _host: str):
        state["held"] = True
        events.append("lock")
        try:
            yield
        finally:
            state["held"] = False

    async def fence(**kwargs: Any) -> None:
        assert state["held"]
        assert kwargs == {"cluster_id": "cluster-a", "db": db}
        events.append("fence")

    class API:
        def put(self, **_kwargs: Any) -> _Response:
            assert state["held"]
            events.append("put")
            return response

    async def get_client(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(api=API())

    def wait(_client: Any, observed: Any, **_kwargs: Any) -> None:
        assert state["held"]
        assert observed is response
        events.append("wait")

    monkeypatch.setattr(lxd_server, "serialize_cluster_configuration", configuration_lock)
    monkeypatch.setattr(lxd_server, "assert_cluster_not_migration_fenced", fence)
    monkeypatch.setattr(lxd_server, "client_for", get_client)
    monkeypatch.setattr(lxd_server, "require_client", lambda value, _host: value)
    monkeypatch.setattr(lxd_server, "wait_for_lxd_operation", wait)
    monkeypatch.setattr(lxd_server, "verify_cluster_access", lambda _host: None)

    result = await lxd_server.update_server_config(
        node_name="cluster-a",
        config={"core.https_allowed_origin": "https://console.example.test"},
        lxd_service=object(),
        _rbac=object(),
        db=db,
    )

    assert result == {}
    assert events == ["lock", "fence", "put", "wait"]
