"""Normal lifecycle paths cannot bypass an active Migration Center fence."""

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from app.routers import integrations, tags, tenant_access
from app.services import migration_fence
from app.services.lxd import instance as instance_module
from app.services.lxd.instance import InstanceManager
from app.services.migration_fence import (
    MIGRATION_FENCE_DETAIL,
    MIGRATION_FENCE_UNAVAILABLE_DETAIL,
    MIGRATION_CLUSTER_FENCE_DETAIL,
    MIGRATION_PROJECT_FENCE_DETAIL,
    MigrationFenceConflict,
)


@pytest.fixture(autouse=True)
def _default_to_no_active_migration_fence() -> None:
    """Override the suite default so these tests exercise the real fence queries."""


class _ScalarResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Any:
        return self.value


class _ScalarRows:
    def __init__(self, values: list[Any]) -> None:
        self.values = values

    def scalars(self) -> list[Any]:
        return self.values


class _Database:
    def __init__(self, *results: Any, error: Exception | None = None) -> None:
        self.results = list(results)
        self.error = error
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> Any:
        self.statements.append(statement)
        if self.error is not None:
            raise self.error
        return self.results.pop(0)


@pytest.mark.asyncio
async def test_active_move_source_is_fenced() -> None:
    db = _Database(_ScalarResult("migration-job-1"))

    with pytest.raises(MigrationFenceConflict) as exc:
        await migration_fence.assert_instance_not_migration_fenced(
            cluster_id="cluster-a",
            project="tenant-a",
            instance_name="web-01",
            db=db,
        )

    assert exc.value.detail == MIGRATION_FENCE_DETAIL
    assert "/inventory/migrations" in exc.value.detail
    assert len(db.statements) == 1


@pytest.mark.asyncio
async def test_disabled_center_cannot_remove_an_existing_durable_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core import config

    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(migration_center_enabled=False),
    )
    db = _Database(_ScalarResult("retained-job"))

    with pytest.raises(MigrationFenceConflict):
        await migration_fence.assert_instance_not_migration_fenced(
            cluster_id="cluster-a",
            project="tenant-a",
            instance_name="web-01",
            db=db,
        )

    assert len(db.statements) == 1


@pytest.mark.asyncio
async def test_enabled_fence_check_fails_closed_without_leaking_database_error(
) -> None:
    db = _Database(error=RuntimeError("postgresql://secret-host/internal"))

    with pytest.raises(MigrationFenceConflict) as exc:
        await migration_fence.assert_instance_not_migration_fenced(
            cluster_id="cluster-a",
            project="tenant-a",
            instance_name="web-01",
            db=db,
        )

    assert exc.value.detail == MIGRATION_FENCE_UNAVAILABLE_DETAIL
    assert "secret-host" not in exc.value.detail


@pytest.mark.asyncio
async def test_resource_operation_reference_is_owned_by_migration(
) -> None:
    db = _Database(
        _ScalarRows([]),
        _ScalarRows(
            [[{"cluster_id": "cluster-a", "operation_id": "operation-1"}]]
        ),
    )

    with pytest.raises(MigrationFenceConflict) as exc:
        await migration_fence.assert_operation_not_migration_owned(
            cluster_id="cluster-a",
            operation_id="operation-1",
            db=db,
        )

    assert exc.value.detail.endswith("/inventory/migrations.")
    assert len(db.statements) == 2


@pytest.mark.asyncio
async def test_staged_target_uses_direction_neutral_resource_lock() -> None:
    db = _Database(_ScalarResult("migration-job-1"))

    with pytest.raises(MigrationFenceConflict):
        await migration_fence.assert_instance_not_migration_fenced(
            cluster_id="target-cluster",
            project="target-project",
            instance_name="web-copy",
            db=db,
        )

    statement = str(db.statements[0])
    assert "migration_resource_locks" in statement
    assert "source_cluster_id" not in statement
    assert "target_cluster_id" not in statement


@pytest.mark.asyncio
async def test_project_dependency_write_uses_any_active_project_lock() -> None:
    db = _Database(_ScalarResult("instance-migration-job"))

    with pytest.raises(MigrationFenceConflict) as exc:
        await migration_fence.assert_project_not_migration_fenced(
            cluster_id="cluster-a",
            project="tenant-a",
            db=db,
        )

    assert exc.value.detail == MIGRATION_PROJECT_FENCE_DETAIL
    statement = str(db.statements[0])
    assert "migration_resource_locks" in statement
    assert "lock_scope" in statement
    assert "migration_jobs.scope" not in statement


@pytest.mark.asyncio
async def test_cluster_infrastructure_write_uses_any_active_cluster_lock() -> None:
    db = _Database(_ScalarResult("migration-job"))

    with pytest.raises(MigrationFenceConflict) as exc:
        await migration_fence.assert_cluster_not_migration_fenced(
            cluster_id="cluster-a",
            db=db,
        )

    assert exc.value.detail == MIGRATION_CLUSTER_FENCE_DETAIL
    statement = str(db.statements[0])
    assert "migration_resource_locks" in statement
    assert "cluster_id" in statement


@pytest.mark.asyncio
async def test_organization_membership_write_checks_every_bound_cluster() -> None:
    db = _Database(
        _ScalarRows(["cluster-a"]),
        _ScalarResult("migration-job"),
    )

    with pytest.raises(MigrationFenceConflict) as exc:
        await migration_fence.assert_organization_not_migration_fenced(
            organization_id=17,
            db=db,
        )

    assert exc.value.detail == MIGRATION_CLUSTER_FENCE_DETAIL
    assert len(db.statements) == 2


class _Instance:
    name = "web-01"
    status = "Stopped"


class _Client:
    pass


def _manager() -> InstanceManager:
    manager = InstanceManager.__new__(InstanceManager)
    manager.connection_manager = SimpleNamespace()
    return manager


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["start_vm", "restart_vm", "delete_vm"])
async def test_instance_lifecycle_checks_fence_before_daemon_mutation(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    manager = _manager()
    instance = _Instance()
    monkeypatch.setattr(manager, "_get_client_with_reconnect", lambda _host: _Client())
    monkeypatch.setattr(manager, "_get_node_endpoint", lambda _host: "https://lxd.invalid")
    monkeypatch.setattr(
        instance_module,
        "find_instance_in_projects",
        lambda _client, _name, _project: (instance, "tenant-a"),
    )

    async def _fenced(**_kwargs: Any) -> None:
        raise MigrationFenceConflict()

    monkeypatch.setattr(instance_module, "assert_instance_not_migration_fenced", _fenced)
    monkeypatch.setattr(
        manager,
        "_start_and_wait",
        lambda *_args: pytest.fail("start reached the daemon after a fence refusal"),
    )

    with pytest.raises(MigrationFenceConflict):
        await getattr(manager, action)("web-01", "cluster-a", project="tenant-a")


@pytest.mark.asyncio
async def test_rebuild_checks_fence_before_daemon_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager()
    monkeypatch.setattr(manager, "_get_client_with_reconnect", lambda _host: _Client())
    monkeypatch.setattr(
        instance_module,
        "find_instance_in_projects",
        lambda _client, _name, _project: (_Instance(), "tenant-a"),
    )

    async def _fenced(**_kwargs: Any) -> None:
        raise MigrationFenceConflict()

    monkeypatch.setattr(instance_module, "assert_instance_not_migration_fenced", _fenced)

    with pytest.raises(MigrationFenceConflict):
        await manager.rebuild_instance(
            "web-01",
            "cluster-a",
            source={"type": "image", "alias": "ubuntu"},
            project="tenant-a",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("creator", ["create_vm", "create_instance"])
async def test_create_checks_project_hierarchy_fence_before_daemon_mutation(
    monkeypatch: pytest.MonkeyPatch,
    creator: str,
) -> None:
    manager = _manager()
    checks: list[tuple[str, str]] = []

    async def _project_fenced(**kwargs: Any) -> None:
        checks.append((kwargs["cluster_id"], kwargs["project"]))
        raise MigrationFenceConflict(MIGRATION_PROJECT_FENCE_DETAIL)

    async def _unexpected_instance_fence(**_kwargs: Any) -> None:
        pytest.fail("exact instance fence ran after the project fence refused the create")

    async def _unexpected_run_blocking(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("create reached the daemon after a project hierarchy fence refusal")

    monkeypatch.setattr(instance_module, "assert_project_not_migration_fenced", _project_fenced)
    monkeypatch.setattr(
        instance_module,
        "assert_instance_not_migration_fenced",
        _unexpected_instance_fence,
    )
    monkeypatch.setattr(instance_module, "run_blocking", _unexpected_run_blocking)

    with pytest.raises(MigrationFenceConflict) as exc:
        await getattr(manager, creator)(
            "web-copy",
            "target-cluster",
            image="ubuntu",
            project="target-project",
        )

    assert exc.value.detail == MIGRATION_PROJECT_FENCE_DETAIL
    assert checks == [("target-cluster", "target-project")]


def test_raw_proxy_recognizes_only_resume_lifecycle_requests() -> None:
    start = integrations.RawProxyRequest(
        cluster_id="cluster-a",
        project="tenant-a",
        method="PUT",
        path="/1.0/instances/web-01/state",
        body={"action": "start"},
    )
    stop = start.model_copy(update={"body": {"action": "stop"}})
    config_write = start.model_copy(
        update={"path": "/1.0/instances/web-01", "body": {"action": "start"}}
    )

    assert integrations._raw_proxy_fenced_write_target(start) == "web-01"
    assert integrations._raw_proxy_fenced_write_target(stop) is None
    assert integrations._raw_proxy_fenced_write_target(config_write) == "web-01"

    delete = start.model_copy(
        update={"method": "DELETE", "path": "/1.0/instances/web-01", "body": None}
    )
    freeze = start.model_copy(update={"body": {"action": "freeze"}})
    assert integrations._raw_proxy_fenced_write_target(delete) == "web-01"
    assert integrations._raw_proxy_fenced_write_target(freeze) == "web-01"


def test_raw_proxy_recognizes_project_dependency_writes() -> None:
    request = integrations.RawProxyRequest(
        cluster_id="cluster-a",
        project="tenant-a",
        method="PUT",
        path="/1.0/profiles/default",
        body={"config": {}},
    )
    assert integrations._raw_proxy_fenced_project(request, "tenant-a") == "tenant-a"

    image_alias = request.model_copy(
        update={"method": "POST", "path": "/1.0/images/aliases", "body": {"name": "base"}}
    )
    assert integrations._raw_proxy_fenced_project(image_alias, "tenant-a") == "tenant-a"

    project_delete = request.model_copy(
        update={"method": "DELETE", "path": "/1.0/projects/target-a", "body": None}
    )
    assert integrations._raw_proxy_fenced_project(project_delete, "tenant-a") == "target-a"

    instance_stop = request.model_copy(
        update={
            "path": "/1.0/instances/web-01/state",
            "body": {"action": "stop"},
        }
    )
    assert integrations._raw_proxy_fenced_project(instance_stop, "tenant-a") is None

    pool_update = request.model_copy(
        update={"path": "/1.0/storage-pools/local", "body": {"config": {}}}
    )
    assert integrations._raw_proxy_has_cluster_wide_write(pool_update) is True
    assert integrations._raw_proxy_has_cluster_wide_write(instance_stop) is False


@pytest.mark.asyncio
async def test_instance_tag_assignment_uses_exact_instance_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []

    async def _capture(**kwargs: Any) -> None:
        seen.append(kwargs)

    monkeypatch.setattr(tags, "assert_instance_not_migration_fenced", _capture)

    await tags._assert_tag_target_not_migration_fenced(
        "instance",
        "web-01",
        host="cluster-a",
        project="tenant-a",
    )

    assert seen == [
        {
            "cluster_id": "cluster-a",
            "project": "tenant-a",
            "instance_name": "web-01",
        }
    ]


@pytest.mark.asyncio
async def test_policy_rule_mutation_uses_project_hierarchy_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str]] = []

    async def _capture(*, cluster_id: str, project: str, db: Any) -> None:
        seen.append((cluster_id, project))

    monkeypatch.setattr(tenant_access, "assert_project_not_migration_fenced", _capture)
    await tenant_access._assert_rules_not_migration_fenced(
        [SimpleNamespace(cluster_id="cluster-a", project_name="tenant-a")],
        SimpleNamespace(),
    )

    assert seen == [("cluster-a", "tenant-a")]


@pytest.mark.asyncio
async def test_raw_proxy_returns_409_before_resuming_fenced_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.lxd import health

    health.clear("cluster-a")

    async def _refuse(**_kwargs: Any) -> None:
        raise HTTPException(status_code=409, detail=MIGRATION_FENCE_DETAIL)

    monkeypatch.setattr(integrations, "_assert_proxy_write_is_not_fenced", _refuse)
    request = integrations.RawProxyRequest(
        cluster_id="cluster-a",
        project="tenant-a",
        method="PUT",
        path="/1.0/instances/web-01/state",
        body={"action": "restart"},
    )
    api_key = {
        "cluster_permissions": [
            {
                "cluster_id": "cluster-a",
                "projects": ["tenant-a"],
                "permissions": ["write"],
            }
        ]
    }

    with pytest.raises(HTTPException) as exc:
        await integrations.raw_proxy(
            request=request,
            api_key=api_key,
            lxd_service=SimpleNamespace(),
            _=None,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == MIGRATION_FENCE_DETAIL


@pytest.mark.asyncio
async def test_structured_proxy_returns_409_before_resuming_fenced_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.lxd import health

    health.clear("cluster-a")

    async def _refuse(**_kwargs: Any) -> None:
        raise HTTPException(status_code=409, detail=MIGRATION_FENCE_DETAIL)

    monkeypatch.setattr(integrations, "_assert_proxy_write_is_not_fenced", _refuse)
    request = integrations.ProxyRequest(
        cluster_id="cluster-a",
        project="tenant-a",
        operation="start_instance",
        params={"name": "web-01"},
    )
    api_key = {
        "cluster_permissions": [
            {
                "cluster_id": "cluster-a",
                "projects": ["tenant-a"],
                "permissions": ["write"],
            }
        ]
    }

    with pytest.raises(HTTPException) as exc:
        await integrations.proxy_operation(
            request=request,
            api_key=api_key,
            lxd_service=SimpleNamespace(),
            _=None,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == MIGRATION_FENCE_DETAIL
