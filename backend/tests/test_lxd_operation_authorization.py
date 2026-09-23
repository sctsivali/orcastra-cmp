"""Project ownership is proven before an LXD operation is read or cancelled."""

import os
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app.core import org_scope, rbac  # noqa: E402
from app.core.auth import AuthenticatedUser, get_current_user  # noqa: E402
from app.core.blocking import BlockingTimeout  # noqa: E402
from app.core.dependencies import check_read_only_mode  # noqa: E402
from app.routers import lxd_operations  # noqa: E402
from app.services.migration_fence import (  # noqa: E402
    MIGRATION_FENCE_DETAIL,
    MIGRATION_OPERATION_DETAIL,
    MIGRATION_PROJECT_FENCE_DETAIL,
    MigrationFenceConflict,
)
from app.services.lxd_service import get_lxd_service  # noqa: E402

HOST = "nuc1"
OPERATION_ID = "op-17"


class _Operation:
    def __init__(
        self,
        *,
        resources: Any,
        metadata: Any = None,
        klass: str = "task",
        may_cancel: bool = True,
    ) -> None:
        self.id = OPERATION_ID
        setattr(self, "class", klass)
        self.created_at = "2026-09-22T10:00:00Z"
        self.updated_at = "2026-09-22T10:00:02Z"
        self.status = "Running"
        self.status_code = 103
        self.description = "Migrating instance"
        self.err = ""
        self.may_cancel = may_cancel
        self.metadata = metadata or {}
        self.resources = resources
        self.waits: list[Optional[int]] = []
        self.io_calls: list[tuple[str, Any]] = []

    def wait(self, timeout: Optional[int] = None) -> None:
        self.waits.append(timeout)


class _OperationManager:
    def __init__(self, operation: _Operation, lookups: list[str]) -> None:
        self.operation = operation
        self.lookups = lookups

    def get(self, operation_id: str) -> _Operation:
        self.lookups.append(operation_id)
        return self.operation


class _Response:
    def __init__(self, operation: _Operation) -> None:
        self.operation = operation

    def json(self) -> dict[str, Any]:
        metadata = dict(vars(self.operation))
        metadata.pop("waits", None)
        metadata.pop("io_calls", None)
        return {"metadata": metadata}


class _WaitEndpoint:
    def __init__(self, operation: _Operation) -> None:
        self.operation = operation

    def get(self, *, params: dict[str, Any], timeout: Any) -> _Response:
        self.operation.waits.append(params.get("timeout"))
        self.operation.io_calls.append(("wait", timeout))
        return _Response(self.operation)


class _OperationEndpoint:
    def __init__(
        self,
        operation_id: str,
        operation: _Operation,
        lookups: list[str],
        deletes: list[str],
    ) -> None:
        self.operation_id = operation_id
        self.operation = operation
        self.lookups = lookups
        self.deletes = deletes

    @property
    def wait(self) -> _WaitEndpoint:
        return _WaitEndpoint(self.operation)

    def get(self, *, timeout: Any) -> _Response:
        self.lookups.append(self.operation_id)
        self.operation.io_calls.append(("lookup", timeout))
        return _Response(self.operation)

    def delete(self, *, timeout: Any) -> None:
        self.deletes.append(self.operation_id)
        self.operation.io_calls.append(("delete", timeout))


class _OperationAPI:
    def __init__(
        self,
        operation: _Operation,
        lookups: list[str],
        deletes: list[str],
    ) -> None:
        self.operation = operation
        self.lookups = lookups
        self.deletes = deletes
        self.segments: list[str] = []

    def __getitem__(self, operation_id: str) -> _OperationEndpoint:
        self.segments.append(operation_id)
        return _OperationEndpoint(
            operation_id,
            self.operation,
            self.lookups,
            self.deletes,
        )


class _ConnectionManager:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.requests: list[str] = []

    def get_client(self, host: str) -> Any:
        self.requests.append(host)
        return self.client


def _test_client(
    monkeypatch: pytest.MonkeyPatch,
    operation: _Operation,
    *,
    groups: Optional[list[str]] = None,
    allowed_clusters: Optional[set[str]] = None,
    visible_projects: Optional[dict[str, set[str]]] = None,
) -> tuple[TestClient, list[str], list[str], _ConnectionManager]:
    actor_groups = groups or ["role_partner"]
    lookups: list[str] = []
    deletes: list[str] = []
    client = SimpleNamespace(
        operations=_OperationManager(operation, lookups),
        api=SimpleNamespace(operations=_OperationAPI(operation, lookups, deletes)),
    )
    manager = _ConnectionManager(client)
    service = SimpleNamespace(connection_manager=manager)
    user = AuthenticatedUser(
        user_id=f"operator-{'-'.join(actor_groups)}",
        email="operator@example.test",
        groups=actor_groups,
    )

    async def _scope() -> None:
        org_scope._allowed_clusters_var.set(allowed_clusters if allowed_clusters is not None else {HOST})
        org_scope._tenant_acl_var.set(None)
        org_scope._visible_projects_var.set(visible_projects)

    async def _authoritative_access(**_kwargs) -> bool:
        return True

    async def _ordinary_operation(**_kwargs) -> None:
        return None

    monkeypatch.setattr(
        lxd_operations,
        "_authoritative_project_access",
        _authoritative_access,
    )
    monkeypatch.setattr(
        lxd_operations,
        "assert_operation_not_migration_owned",
        _ordinary_operation,
    )

    monkeypatch.setattr(
        rbac,
        "activity_logger",
        SimpleNamespace(log_activity=lambda **_kwargs: None),
    )
    app = FastAPI()
    app.include_router(lxd_operations.router)
    app.dependency_overrides[get_lxd_service] = lambda: service
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[org_scope.org_scope_dependency] = _scope
    app.dependency_overrides[check_read_only_mode] = lambda: None
    return TestClient(app, raise_server_exceptions=False), lookups, deletes, manager


def _instance_resources(project: str = "mine") -> dict[str, list[str]]:
    return {"instances": [f"/1.0/instances/web-01?project={project}"]}


def test_detail_derives_owner_and_returns_only_allowlisted_metadata(monkeypatch) -> None:
    operation = _Operation(
        resources=_instance_resources(),
        metadata={
            "download_progress": "rootfs: 42% token=SYNTHETIC_SENTINEL",
            "progress": 42,
            "fds": {"0": "console-secret"},
            "secrets": {"control": "migration-secret"},
            "command": ["sh", "-c", "private-command"],
            "migration_progress": {"nested": "not-a-safe-scalar"},
        },
    )
    operation.description = "Migrating with ghp_SYNTHETIC_SENTINEL1234567890"
    client, lookups, _deletes, _manager = _test_client(
        monkeypatch,
        operation,
        visible_projects={HOST: {"mine"}},
    )

    response = client.get(f"/operations/{OPERATION_ID}?host={HOST}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["project"] == "mine"
    assert body["instance_name"] == "web-01"
    assert body["node_name"] == HOST
    assert body["metadata"] == {
        "download_progress": "rootfs: 42% token=[redacted]",
        "progress": 42,
    }
    assert body["description"] == "Migrating with [redacted-token]"
    assert "SYNTHETIC_SENTINEL" not in response.text
    assert "console-secret" not in response.text
    assert "migration-secret" not in response.text
    assert "private-command" not in response.text
    assert lookups == [OPERATION_ID]


def test_read_and_cancel_treat_another_projects_operation_as_absent(monkeypatch) -> None:
    operation = _Operation(
        resources=_instance_resources("theirs"),
        metadata={"project": "mine", "fds": {"0": "must-not-leak"}},
    )
    client, _lookups, deletes, _manager = _test_client(
        monkeypatch,
        operation,
        visible_projects={HOST: {"mine"}},
    )

    detail = client.get(f"/operations/{OPERATION_ID}?host={HOST}")
    cancel = client.delete(f"/operations/{OPERATION_ID}?host={HOST}")

    assert detail.status_code == 404, detail.text
    assert cancel.status_code == 404, cancel.text
    assert detail.json() == {"detail": "Operation not found"}
    assert cancel.json() == {"detail": "Operation not found"}
    assert "theirs" not in detail.text
    assert "must-not-leak" not in detail.text
    assert deletes == []


def test_shared_default_project_without_exact_owner_is_absent(monkeypatch) -> None:
    operation = _Operation(resources=_instance_resources("default"))
    client, _lookups, deletes, _manager = _test_client(
        monkeypatch,
        operation,
        visible_projects={HOST: {"default"}},
    )

    async def _ambiguous_default(**_kwargs) -> bool:
        return False

    monkeypatch.setattr(
        lxd_operations,
        "_authoritative_project_access",
        _ambiguous_default,
    )

    detail = client.get(f"/operations/{OPERATION_ID}?host={HOST}")
    cancel = client.delete(f"/operations/{OPERATION_ID}?host={HOST}")
    wait = client.post(f"/operations/{OPERATION_ID}/wait?host={HOST}")

    assert detail.status_code == 404
    assert cancel.status_code == 404
    assert wait.status_code == 404
    assert deletes == []


@pytest.mark.asyncio
async def test_partner_shared_default_requires_exact_database_owner(monkeypatch) -> None:
    class _SessionContext:
        async def __aenter__(self) -> Any:
            return SimpleNamespace()

        async def __aexit__(self, *_args: Any) -> None:
            return None

    async def _ambiguous_owner(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(
            organization_id=None,
            organization_slug=None,
            resolution="ambiguous",
        )

    monkeypatch.setattr(lxd_operations, "async_session_maker", _SessionContext)
    monkeypatch.setattr(
        lxd_operations,
        "resolve_instance_job_organization",
        _ambiguous_owner,
    )
    actor = AuthenticatedUser(
        user_id="partner-1",
        email="partner@example.test",
        groups=["role_partner"],
    )

    assert await lxd_operations._authoritative_project_access(
        host=HOST,
        project="default",
        actor=actor,
    ) is False


@pytest.mark.parametrize(
    "resources",
    [
        {},
        {
            "instances": [
                "/1.0/instances/web-01?project=mine",
                "/1.0/instances/web-02?project=theirs",
            ]
        },
        {"certificates": ["/1.0/certificates/fingerprint?project=mine"]},
        {"instances": ["https://other.invalid/1.0/instances/web-01?project=mine"]},
        {"instances": ["/1.0/instances/web-01?project=mine&project=theirs"]},
        {"instances": ["/1.0/instances/web-01?project="]},
    ],
)
def test_missing_or_ambiguous_resource_ownership_fails_closed(monkeypatch, resources) -> None:
    operation = _Operation(resources=resources)
    client, _lookups, deletes, _manager = _test_client(
        monkeypatch,
        operation,
        visible_projects={HOST: {"mine", "theirs"}},
    )

    detail = client.get(f"/operations/{OPERATION_ID}?host={HOST}")
    cancel = client.delete(f"/operations/{OPERATION_ID}?host={HOST}")

    assert detail.status_code == 404, detail.text
    assert cancel.status_code == 404, cancel.text
    assert deletes == []


def test_authorized_task_cancellation_reads_owner_before_delete(monkeypatch) -> None:
    operation = _Operation(resources=_instance_resources())
    client, lookups, deletes, _manager = _test_client(
        monkeypatch,
        operation,
        visible_projects={HOST: {"mine"}},
    )

    response = client.delete(f"/operations/{OPERATION_ID}?host={HOST}")

    assert response.status_code == 200, response.text
    assert response.json()["success"] is True
    assert lookups == [OPERATION_ID]
    assert deletes == [OPERATION_ID]


@pytest.mark.parametrize(
    ("method", "expected_segment_uses"),
    [("get", 1), ("delete", 2), ("wait", 2)],
)
def test_operation_id_is_encoded_once_at_every_daemon_path(
    monkeypatch,
    method: str,
    expected_segment_uses: int,
) -> None:
    operation = _Operation(resources=_instance_resources())
    client, _lookups, _deletes, manager = _test_client(
        monkeypatch,
        operation,
        visible_projects={HOST: {"mine"}},
    )
    path = f"/operations/op%3Fproject%3Dtheirs?host={HOST}"

    if method == "get":
        response = client.get(path)
    elif method == "delete":
        response = client.delete(path)
    else:
        response = client.post(
            f"/operations/op%3Fproject%3Dtheirs/wait?host={HOST}&timeout=2"
        )

    assert response.status_code == 200, response.text
    assert manager.client.api.operations.segments == [
        "op%3Fproject%3Dtheirs"
    ] * expected_segment_uses


def test_detail_cancel_and_wait_use_bounded_offload_with_socket_timeouts(monkeypatch) -> None:
    operation = _Operation(resources=_instance_resources())
    client, _lookups, _deletes, _manager = _test_client(
        monkeypatch,
        operation,
        visible_projects={HOST: {"mine"}},
    )
    offloads: list[dict[str, Any]] = []

    async def _offload(call, **kwargs):
        offloads.append(kwargs)
        return call()

    monkeypatch.setattr(lxd_operations, "run_blocking", _offload)

    assert client.get(f"/operations/{OPERATION_ID}?host={HOST}").status_code == 200
    assert client.delete(f"/operations/{OPERATION_ID}?host={HOST}").status_code == 200
    assert client.post(
        f"/operations/{OPERATION_ID}/wait?host={HOST}&timeout=2"
    ).status_code == 200

    assert [call["label"] for call in offloads] == [
        f"operations.lookup[{HOST}]",
        f"operations.lookup[{HOST}]",
        f"operations.cancel[{HOST}]",
        f"operations.lookup[{HOST}]",
        f"operations.wait[{HOST}]",
    ]
    assert all(call["executor"] is lxd_operations._operation_executor for call in offloads)
    assert all(call["max_in_flight"] == 8 for call in offloads)
    assert all(call["concurrency_key"] == HOST for call in offloads)
    assert all(call["max_in_flight_per_key"] == 4 for call in offloads)
    assert operation.io_calls == [
        ("lookup", (3, 8)),
        ("lookup", (3, 8)),
        ("delete", (3, 8)),
        ("lookup", (3, 8)),
        ("wait", (3, 7)),
    ]
    assert operation.waits == [2]


@pytest.mark.parametrize("method", ["get", "delete", "wait"])
def test_lookup_timeout_is_concealed_as_not_found(monkeypatch, method: str) -> None:
    operation = _Operation(resources=_instance_resources())
    client, _lookups, deletes, _manager = _test_client(
        monkeypatch,
        operation,
        visible_projects={HOST: {"mine"}},
    )

    async def _timeout(_call, **kwargs):
        raise BlockingTimeout("private lookup detail", kwargs["timeout"])

    monkeypatch.setattr(lxd_operations, "run_blocking", _timeout)
    url = f"/operations/{OPERATION_ID}?host={HOST}"
    if method == "get":
        response = client.get(url)
    elif method == "delete":
        response = client.delete(url)
    else:
        response = client.post(f"/operations/{OPERATION_ID}/wait?host={HOST}&timeout=2")

    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "Operation not found"}
    assert "private lookup detail" not in response.text
    assert deletes == []


def test_cancel_timeout_reports_unknown_without_leaking_failure_detail(monkeypatch) -> None:
    operation = _Operation(resources=_instance_resources())
    client, _lookups, deletes, _manager = _test_client(
        monkeypatch,
        operation,
        visible_projects={HOST: {"mine"}},
    )

    async def _timeout_cancel(call, **kwargs):
        if kwargs["label"].startswith("operations.cancel"):
            raise BlockingTimeout("private daemon address", kwargs["timeout"])
        return call()

    monkeypatch.setattr(lxd_operations, "run_blocking", _timeout_cancel)
    response = client.delete(f"/operations/{OPERATION_ID}?host={HOST}")

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "operation_cancel_outcome_unknown"
    assert "private daemon address" not in response.text
    assert deletes == []


def test_wait_timeout_is_sanitized_and_does_not_claim_completion(monkeypatch) -> None:
    operation = _Operation(resources=_instance_resources())
    client, _lookups, _deletes, _manager = _test_client(
        monkeypatch,
        operation,
        visible_projects={HOST: {"mine"}},
    )

    async def _timeout_wait(call, **kwargs):
        if kwargs["label"].startswith("operations.wait"):
            raise BlockingTimeout("private daemon address", kwargs["timeout"])
        return call()

    monkeypatch.setattr(lxd_operations, "run_blocking", _timeout_wait)
    response = client.post(f"/operations/{OPERATION_ID}/wait?host={HOST}&timeout=2")

    assert response.status_code == 503, response.text
    assert response.json() == {
        "detail": "Operation wait is temporarily unavailable; refresh and retry."
    }
    assert "private daemon address" not in response.text
    assert operation.waits == []


def test_migration_operation_cannot_be_cancelled_from_legacy_endpoint(monkeypatch) -> None:
    operation = _Operation(resources=_instance_resources())
    client, lookups, deletes, _manager = _test_client(
        monkeypatch,
        operation,
        visible_projects={HOST: {"mine"}},
    )

    async def _migration_owned(**_kwargs) -> None:
        raise MigrationFenceConflict(MIGRATION_OPERATION_DETAIL)

    monkeypatch.setattr(
        lxd_operations,
        "assert_operation_not_migration_owned",
        _migration_owned,
    )

    response = client.delete(f"/operations/{OPERATION_ID}?host={HOST}")

    assert response.status_code == 409, response.text
    assert response.json() == {"detail": MIGRATION_OPERATION_DETAIL}
    assert lookups == [OPERATION_ID]
    assert deletes == []


@pytest.mark.parametrize(
    ("fenced_scope", "expected_detail", "expected_checks"),
    [
        ("project", MIGRATION_PROJECT_FENCE_DETAIL, ["project"]),
        ("instance", MIGRATION_FENCE_DETAIL, ["project", "instance"]),
    ],
)
def test_cancel_checks_migration_resource_fences_before_daemon_delete(
    monkeypatch,
    fenced_scope: str,
    expected_detail: str,
    expected_checks: list[str],
) -> None:
    operation = _Operation(resources=_instance_resources())
    client, lookups, deletes, _manager = _test_client(
        monkeypatch,
        operation,
        visible_projects={HOST: {"mine"}},
    )
    checks: list[str] = []

    async def project_fence(**kwargs) -> None:
        checks.append("project")
        assert kwargs == {"cluster_id": HOST, "project": "mine"}
        if fenced_scope == "project":
            raise MigrationFenceConflict(MIGRATION_PROJECT_FENCE_DETAIL)

    async def instance_fence(**kwargs) -> None:
        checks.append("instance")
        assert kwargs == {
            "cluster_id": HOST,
            "project": "mine",
            "instance_name": "web-01",
        }
        if fenced_scope == "instance":
            raise MigrationFenceConflict(MIGRATION_FENCE_DETAIL)

    async def operation_fence(**_kwargs) -> None:
        pytest.fail("operation journal check ran after a resource fence refusal")

    monkeypatch.setattr(lxd_operations, "assert_project_not_migration_fenced", project_fence)
    monkeypatch.setattr(lxd_operations, "assert_instance_not_migration_fenced", instance_fence)
    monkeypatch.setattr(lxd_operations, "assert_operation_not_migration_owned", operation_fence)

    response = client.delete(f"/operations/{OPERATION_ID}?host={HOST}")

    assert response.status_code == 409, response.text
    assert response.json() == {"detail": expected_detail}
    assert checks == expected_checks
    assert lookups == [OPERATION_ID]
    assert deletes == []


@pytest.mark.parametrize(
    ("klass", "may_cancel"),
    [("websocket", True), ("task", False), ("", True)],
)
def test_cancel_refuses_unsafe_operation_classes_and_daemon_verdicts(
    monkeypatch,
    klass: str,
    may_cancel: bool,
) -> None:
    operation = _Operation(
        resources=_instance_resources(),
        klass=klass,
        may_cancel=may_cancel,
    )
    client, _lookups, deletes, _manager = _test_client(
        monkeypatch,
        operation,
        visible_projects={HOST: {"mine"}},
    )

    response = client.delete(f"/operations/{OPERATION_ID}?host={HOST}")

    assert response.status_code == 409, response.text
    assert deletes == []


def test_wait_cannot_be_used_as_a_foreign_operation_read(monkeypatch) -> None:
    operation = _Operation(resources=_instance_resources("theirs"))
    client, _lookups, _deletes, _manager = _test_client(
        monkeypatch,
        operation,
        visible_projects={HOST: {"mine"}},
    )

    response = client.post(f"/operations/{OPERATION_ID}/wait?host={HOST}&timeout=2")

    assert response.status_code == 404, response.text
    assert operation.waits == []


def test_tenant_is_denied_before_the_cluster_is_read(monkeypatch) -> None:
    operation = _Operation(resources=_instance_resources())
    client, lookups, deletes, manager = _test_client(
        monkeypatch,
        operation,
        groups=["role_tenant"],
        visible_projects={HOST: {"mine"}},
    )

    detail = client.get(f"/operations/{OPERATION_ID}?host={HOST}")
    cancel = client.delete(f"/operations/{OPERATION_ID}?host={HOST}")

    assert detail.status_code == 403, detail.text
    assert cancel.status_code == 403, cancel.text
    assert manager.requests == []
    assert lookups == []
    assert deletes == []


def test_foreign_cluster_is_denied_before_the_cluster_is_read(monkeypatch) -> None:
    operation = _Operation(resources=_instance_resources())
    client, lookups, _deletes, manager = _test_client(
        monkeypatch,
        operation,
        allowed_clusters={"other-cluster"},
        visible_projects={HOST: {"mine"}},
    )

    response = client.get(f"/operations/{OPERATION_ID}?host={HOST}")

    assert response.status_code == 403, response.text
    assert manager.requests == []
    assert lookups == []


def test_project_resource_uses_the_project_name_as_its_owner(monkeypatch) -> None:
    operation = _Operation(resources={"projects": ["/1.0/projects/mine"]})
    client, _lookups, _deletes, _manager = _test_client(
        monkeypatch,
        operation,
        visible_projects={HOST: {"mine"}},
    )

    response = client.get(f"/operations/{OPERATION_ID}?host={HOST}")

    assert response.status_code == 200, response.text
    assert response.json()["project"] == "mine"
    assert response.json()["instance_name"] == ""


def test_list_payload_derives_non_instance_project_instead_of_defaulting() -> None:
    ownership = lxd_operations._operation_ownership(
        {
            "resources": {
                "networks": ["/1.0/networks/uplink?project=theirs"],
            }
        }
    )

    assert ownership is not None
    assert ownership.project == "theirs"
    assert ownership.instance_name == ""


def test_list_payload_drops_unknown_or_mixed_resource_ownership() -> None:
    assert lxd_operations._operation_ownership(
        {"resources": {"certificates": ["/1.0/certificates/fingerprint"]}}
    ) is None
    assert lxd_operations._operation_ownership(
        {
            "resources": {
                "instances": ["/1.0/instances/a?project=mine"],
                "networks": ["/1.0/networks/uplink?project=theirs"],
            }
        }
    ) is None
