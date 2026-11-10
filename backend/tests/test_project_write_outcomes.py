"""Project writes reconcile ambiguous daemon outcomes without replaying mutations."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from contextlib import asynccontextmanager
from threading import Lock
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from app.core.blocking import BlockingTimeout
from app.core.auth import AuthenticatedUser
from app.core.rbac import Role
from app.models.schemas import ProjectCreateRequest
from app.routers import projects as projects_router
from app.services.lxd import projects as projects_module
from app.services.instance_security_policy import ClusterOwnership, ClusterWritePolicy
from app.services.lxd.exceptions import (
    LXDConnectionError,
    LXDOperationError,
    LXDOperationUnknownError,
)
from app.services.lxd.projects import ProjectManager


class _Response:
    def __init__(self, metadata: Any = None) -> None:
        self._metadata = metadata

    def json(self) -> dict[str, Any]:
        return {"metadata": deepcopy(self._metadata)}


class _ProjectEndpoint:
    def __init__(self, daemon: "_ProjectDaemon", name: str) -> None:
        self._daemon = daemon
        self._name = name

    def get(self, *, timeout: tuple[int, int]) -> _Response:
        with self._daemon.lock:
            self._daemon.read_calls.append((self._name, timeout))
            record = deepcopy(self._daemon.records[self._name])
        return _Response(record)

    def put(self, *, json: dict[str, Any], timeout: tuple[int, int]) -> _Response:
        with self._daemon.lock:
            self._daemon.put_calls.append((self._name, deepcopy(json), timeout))
            if self._daemon.apply_updates:
                current = self._daemon.records[self._name]
                current["description"] = json["description"]
                current["config"] = deepcopy(json["config"])
                # This value can only come from the terminal daemon readback. It proves the
                # service did not report the request payload as the resulting project record.
                current["config"]["user.daemon-readback"] = "confirmed"
        return _Response()


class _ProjectsEndpoint:
    def __init__(self, daemon: "_ProjectDaemon") -> None:
        self._daemon = daemon

    def __getitem__(self, name: str) -> _ProjectEndpoint:
        return _ProjectEndpoint(self._daemon, name)

    def post(self, *, json: dict[str, Any], timeout: tuple[int, int]) -> _Response:
        with self._daemon.lock:
            self._daemon.post_calls.append((deepcopy(json), timeout))
            self._daemon.events.append("post-started")
            if self._daemon.refuse_duplicates and json["name"] in self._daemon.records:
                self._daemon.events.append("duplicate-refused")
                raise RuntimeError("duplicate project")

        record = {
            "name": json["name"],
            "description": json["description"],
            "config": deepcopy(json["config"]),
            "used_by": [],
        }
        if self._daemon.create_mismatch:
            record["config"]["restricted"] = "false"

        with self._daemon.lock:
            self._daemon.records[json["name"]] = record
            self._daemon.events.append("post-finished")
        return _Response()


class _ProjectDaemon:
    def __init__(
        self,
        *,
        create_mismatch: bool = False,
        apply_updates: bool = True,
        refuse_duplicates: bool = False,
    ) -> None:
        self.create_mismatch = create_mismatch
        self.apply_updates = apply_updates
        self.refuse_duplicates = refuse_duplicates
        self.lock = Lock()
        self.events: list[str] = []
        self.records: dict[str, dict[str, Any]] = {}
        self.post_calls: list[tuple[dict[str, Any], tuple[int, int]]] = []
        self.put_calls: list[tuple[str, dict[str, Any], tuple[int, int]]] = []
        self.read_calls: list[tuple[str, tuple[int, int]]] = []
        self.client = SimpleNamespace(
            api=SimpleNamespace(
                projects=_ProjectsEndpoint(self),
                operations=SimpleNamespace(),
            )
        )

    def manager(self) -> ProjectManager:
        connection_manager = SimpleNamespace(get_client=lambda _host: self.client)
        return ProjectManager(connection_manager)


SAFE_CONFIG = {
    "restricted": "true",
    "features.images": "true",
    "features.networks": "true",
    "features.profiles": "true",
    "features.storage.volumes": "true",
}


def _expire_only_the_initial_write(
    monkeypatch: pytest.MonkeyPatch,
    *,
    host: str,
    operation: str,
) -> dict[str, Any]:
    observed: dict[str, Any] = {
        "labels": [],
        "expired_while_running": False,
        "worker_completed_after_timeout": False,
    }

    async def run_with_initial_timeout(function, *args, **kwargs):
        label = kwargs["label"]
        observed["labels"].append(label)
        if label != f"project.{operation}[{host}]":
            return function(*args)

        await asyncio.sleep(0.01)
        observed["expired_while_running"] = True
        # A wall timeout stops waiting for the result but leaves the thread running. The
        # ownership lock may proceed only after that worker has finished.
        try:
            await asyncio.sleep(0.04)
            function(*args)
        except Exception:
            pass
        observed["worker_completed_after_timeout"] = True
        raise BlockingTimeout(label, 0.01)

    monkeypatch.setattr(projects_module, "run_blocking", run_with_initial_timeout)
    return observed


@pytest.mark.asyncio
async def test_create_timeout_reconciles_late_success_without_replaying_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = "late-create-success"
    daemon = _ProjectDaemon()
    manager = daemon.manager()
    observed = _expire_only_the_initial_write(
        monkeypatch, host=host, operation="create"
    )

    project = await manager.create_project(
        host,
        "tenant-a",
        description="Tenant A",
        config=SAFE_CONFIG,
        max_cpu=4,
        max_ram="4GiB",
        max_storage="40GiB",
    )

    assert project == {
        "name": "tenant-a",
        "description": "Tenant A",
        "config": {
            **SAFE_CONFIG,
            "limits.cpu": "4",
            "limits.memory": "4GiB",
            "limits.disk": "40GiB",
        },
        "used_by": [],
        "host": host,
    }
    assert daemon.events == ["post-started", "post-finished"]
    assert observed["expired_while_running"] is True
    assert observed["worker_completed_after_timeout"] is True
    assert observed["labels"] == [
        f"project.create[{host}]",
        f"project.create-reconcile[{host}]",
    ]
    assert len(daemon.post_calls) == 1, "an ambiguous create must never be submitted twice"
    assert len(daemon.read_calls) >= 2, "late completion must be proven by a fresh readback"


@pytest.mark.asyncio
async def test_create_timeout_with_mismatched_readback_is_unknown_not_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = "late-create-mismatch"
    daemon = _ProjectDaemon(create_mismatch=True)
    manager = daemon.manager()
    observed = _expire_only_the_initial_write(
        monkeypatch, host=host, operation="create"
    )

    with pytest.raises(LXDOperationUnknownError):
        await manager.create_project(
            host,
            "tenant-b",
            description="Tenant B",
            config=SAFE_CONFIG,
            max_cpu=2,
            max_ram="2GiB",
            max_storage="20GiB",
        )

    assert len(daemon.post_calls) == 1
    assert len(daemon.read_calls) >= 2
    assert observed["expired_while_running"] is True
    assert observed["worker_completed_after_timeout"] is True
    assert daemon.records["tenant-b"]["config"]["restricted"] == "false"


@pytest.mark.asyncio
async def test_create_ambiguous_then_reconciliation_disconnect_stays_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = "create-reconcile-disconnect"
    manager = _ProjectDaemon().manager()
    labels: list[str] = []

    async def ambiguous_then_disconnect(function, *args, **kwargs):
        label = kwargs["label"]
        labels.append(label)
        if label == f"project.create[{host}]":
            raise BlockingTimeout(label, 0.01)
        raise LXDConnectionError("cluster became unavailable during reconciliation")

    monkeypatch.setattr(
        projects_module,
        "run_blocking",
        ambiguous_then_disconnect,
    )

    with pytest.raises(LXDOperationUnknownError) as caught:
        await manager.create_project(
            host,
            "tenant-disconnected",
            description="Tenant disconnected",
            config=SAFE_CONFIG,
        )

    assert isinstance(caught.value.__cause__, LXDConnectionError)
    assert labels == [
        f"project.create[{host}]",
        f"project.create-reconcile[{host}]",
    ]


@pytest.mark.asyncio
async def test_update_preserves_unrelated_config_and_returns_verified_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = "update-readback"
    daemon = _ProjectDaemon()
    daemon.records["tenant-c"] = {
        "name": "tenant-c",
        "description": "Old description",
        "config": {
            **SAFE_CONFIG,
            "user.owner": "platform",
            "limits.cpu": "2",
            "limits.memory": "2GiB",
            "limits.disk": "20GiB",
        },
        "used_by": ["/1.0/instances/example"],
    }
    manager = daemon.manager()

    async def run_inline(function, *args, **_kwargs):
        return function(*args)

    monkeypatch.setattr(projects_module, "run_blocking", run_inline)

    project = await manager.update_project(
        host,
        "tenant-c",
        description="New description",
        max_cpu=6,
        max_ram="0",
        max_storage=None,
    )

    assert len(daemon.put_calls) == 1
    _name, submitted, _timeout = daemon.put_calls[0]
    assert submitted == {
        "description": "New description",
        "config": {
            **SAFE_CONFIG,
            "user.owner": "platform",
            "limits.cpu": "6",
            "limits.disk": "20GiB",
        },
    }
    assert len(daemon.read_calls) == 2
    assert project["description"] == "New description"
    assert project["config"]["user.owner"] == "platform"
    assert project["config"]["user.daemon-readback"] == "confirmed"
    assert "limits.memory" not in project["config"]


@pytest.mark.asyncio
async def test_update_ambiguous_then_reconciliation_disconnect_stays_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = "update-reconcile-disconnect"
    manager = _ProjectDaemon().manager()
    labels: list[str] = []

    async def ambiguous_then_disconnect(function, *args, **kwargs):
        label = kwargs["label"]
        labels.append(label)
        if label == f"project.update[{host}]":
            raise BlockingTimeout(label, 0.01)
        raise LXDConnectionError("cluster became unavailable during reconciliation")

    monkeypatch.setattr(
        projects_module,
        "run_blocking",
        ambiguous_then_disconnect,
    )

    with pytest.raises(LXDOperationUnknownError) as caught:
        await manager.update_project(
            host,
            "tenant-disconnected",
            description="Updated",
        )

    assert isinstance(caught.value.__cause__, LXDConnectionError)
    assert labels == [
        f"project.update[{host}]",
        f"project.update-reconcile[{host}]",
    ]


def test_update_acknowledged_but_not_applied_is_an_unknown_outcome() -> None:
    daemon = _ProjectDaemon(apply_updates=False)
    daemon.records["tenant-d"] = {
        "name": "tenant-d",
        "description": "Old description",
        "config": {**SAFE_CONFIG, "limits.cpu": "2"},
        "used_by": [],
    }
    manager = daemon.manager()

    with pytest.raises(LXDOperationUnknownError):
        manager._update_project_sync(
            "update-mismatch",
            "tenant-d",
            "New description",
            8,
            None,
            None,
        )

    assert len(daemon.put_calls) == 1
    assert len(daemon.read_calls) == 2


@pytest.mark.asyncio
async def test_route_does_not_grant_a_project_until_create_is_proven(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = "shared-create-unknown"
    grants: list[dict[str, Any]] = []
    daemon = _ProjectDaemon(refuse_duplicates=True)
    daemon.records["tenant-e"] = {
        "name": "tenant-e",
        "description": "Tenant E",
        "config": {
            **SAFE_CONFIG,
            "limits.cpu": "2",
            "limits.memory": "2GiB",
            "limits.disk": "20GiB",
        },
        "used_by": [],
    }
    manager = daemon.manager()
    observed = _expire_only_the_initial_write(
        monkeypatch, host=host, operation="create"
    )

    @asynccontextmanager
    async def no_op_lock(*_args, **_kwargs):
        yield

    async def shared_policy(*_args, **_kwargs):
        return ClusterWritePolicy(host, Role.PARTNER, ClusterOwnership.SHARED)

    async def capacity_unavailable(*_args, **_kwargs):
        raise LXDOperationError("capacity unavailable")

    async def grant_owner(*_args, **_kwargs):
        return 73

    async def record_grant(*_args, **kwargs):
        grants.append(kwargs)
        return ""

    class _Service:
        @staticmethod
        async def create_project(**kwargs):
            return await manager.create_project(**kwargs)

    monkeypatch.setattr(projects_router, "verify_cluster_access", lambda _host: None)
    monkeypatch.setattr(projects_router, "_bounded_node_resources", capacity_unavailable)
    monkeypatch.setattr(
        projects_router, "serialize_cluster_configuration", no_op_lock
    )
    monkeypatch.setattr(projects_router, "_cluster_write_policy", shared_policy)
    monkeypatch.setattr(projects_router, "org_to_hold_project_grant", grant_owner)
    monkeypatch.setattr(
        projects_router.project_grants, "record_created_project", record_grant
    )
    monkeypatch.setattr(
        projects_router,
        "activity_logger",
        SimpleNamespace(log_activity=lambda **_kwargs: None),
    )

    request = ProjectCreateRequest(
        name="tenant-e",
        host=host,
        description="Tenant E",
        config=SAFE_CONFIG,
        max_cpu=2,
        max_ram="2GiB",
        max_storage="20GiB",
    )
    user = AuthenticatedUser(
        user_id="partner-73",
        email="partner-73@example.test",
        roles=["role_partner"],
        groups=["role_partner"],
    )

    with pytest.raises(HTTPException) as caught:
        await projects_router.create_project(
            request,
            lxd_service=_Service(),
            _=None,
            user=user,
            db=SimpleNamespace(),
        )

    assert caught.value.status_code == 503
    assert caught.value.detail["code"] == "project_write_unknown"
    assert grants == []
    assert observed["worker_completed_after_timeout"] is True
    assert daemon.events == ["post-started", "duplicate-refused"]
    assert projects_module._CREATE_CORRELATION_KEY not in daemon.records["tenant-e"]["config"]
