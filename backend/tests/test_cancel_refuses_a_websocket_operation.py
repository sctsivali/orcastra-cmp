"""`DELETE /tasks/{id}` refuses what it must and cancels what it may, audited.

Drives the real router through a real app (the wiring of `test_unreachable_cluster_is_503`),
with the session, the scope and the LXD client faked at their boundaries. Asserted: the
refusal codes the panel branches on (a websocket-class operation is routed to "Kill Console
Session"), the enumeration rule for a row outside the caller's scope, the DELETE reaching the
cluster with a per-attempt socket timeout, the `task.cancel` audit record, and a dead cluster
answering 503 rather than 500.
"""

import contextvars
import os
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:1/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from _journal_fakes import FakeDB, FakeResult  # noqa: E402
from app.core import org_scope  # noqa: E402
from app.core.auth import AuthenticatedUser  # noqa: E402
from app.core.database import get_db  # noqa: E402
from app.core.rbac import get_current_user  # noqa: E402
from app.routers import tasks as tasks_router  # noqa: E402
from app.services.lxd.lxd_service import LXDService, get_lxd_service  # noqa: E402

TASK_ID = "8d1b2c1e-0000-4000-8000-000000000001"


def _row(**overrides: Any) -> Dict[str, Any]:
    row = {
        "id": TASK_ID, "seq": 3, "kind": "lxd_operation", "cluster_id": "nuc1",
        "project": "cust-a", "resource_type": "instance", "resource_name": "web-01",
        "operation_id": "op-77", "operation_class": "task", "action": "instance.start",
        "description": "Starting instance", "status": "running", "status_code": 103,
        "progress": None, "error": None, "may_cancel": True, "actor_email": "ops@example.test",
        "actor_role": "partner", "actor_kind": "user", "api_key_id": None, "source": "dashboard",
        "request_id": None, "target_organization_id": None, "target_organization_slug": None,
        "metadata": {}, "lxd_created_at": None, "lxd_updated_at": None, "started_at": None,
        "finished_at": None, "created_at": "2026-09-19T00:00:00+00:00",
        "updated_at": "2026-09-19T00:00:00+00:00",
    }
    row.update(overrides)
    return row


class _Endpoint:
    def __init__(self, operation_id: str, calls: List[tuple], raises: Optional[Exception]):
        self.operation_id = operation_id
        self.calls = calls
        self.raises = raises

    def delete(self, **kwargs: Any) -> None:
        self.calls.append((self.operation_id, kwargs.get("timeout")))
        if self.raises is not None:
            raise self.raises


class _Operations:
    def __init__(self, calls: List[tuple], raises: Optional[Exception]):
        self.calls = calls
        self.raises = raises

    def __getitem__(self, operation_id: str) -> _Endpoint:
        return _Endpoint(operation_id, self.calls, self.raises)


class _Connection:
    def __init__(self, client: Any):
        self.client = client

    def get_client(self, host: str, *args: Any, **kwargs: Any) -> Any:
        return self.client


def _service(calls: List[tuple], *, reachable: bool = True, raises: Optional[Exception] = None) -> LXDService:
    svc = LXDService.__new__(LXDService)
    client = SimpleNamespace(api=SimpleNamespace(operations=_Operations(calls, raises)))
    svc.connection_manager = _Connection(client if reachable else None)
    svc.cert_manager = None
    return svc


class _Audit:
    def __init__(self) -> None:
        self.records: List[Dict[str, Any]] = []

    def log_activity(self, **kwargs: Any) -> str:
        self.records.append(kwargs)
        return "evt"


def _client(
    monkeypatch,
    row: Optional[Dict[str, Any]],
    *,
    groups: List[str],
    allowed: Optional[set],
    acl: Optional[dict] = None,
    calls: Optional[List[tuple]] = None,
    reachable: bool = True,
    raises: Optional[Exception] = None,
    audit: Optional[_Audit] = None,
) -> TestClient:
    def respond(sql: str, _params: Any) -> Optional[FakeResult]:
        if "FROM task_journal" in sql:
            return FakeResult(rows=[row] if row else [])
        return FakeResult(rows=[])

    async def _db():
        yield FakeDB(respond)

    user = AuthenticatedUser(
        user_id=f"uid-{'-'.join(groups)}", email="ops@example.test", groups=groups
    )

    async def _scope() -> None:
        org_scope._allowed_clusters_var.set(allowed)
        org_scope._tenant_acl_var.set(acl)
        org_scope._visible_projects_var.set(None)

    monkeypatch.setattr(tasks_router, "get_activity_logger", lambda: audit or _Audit())
    app = FastAPI()
    app.include_router(tasks_router.router)
    app.dependency_overrides[get_lxd_service] = lambda: _service(
        calls if calls is not None else [], reachable=reachable, raises=raises
    )
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[org_scope.org_scope_dependency] = _scope
    app.dependency_overrides[get_db] = _db
    return TestClient(app, raise_server_exceptions=False)


PARTNER = ["role_partner"]


def test_a_websocket_class_operation_is_refused_and_routed_to_the_sessions_page(monkeypatch) -> None:
    calls: List[tuple] = []
    client = _client(
        monkeypatch, _row(operation_class="websocket", action="instance.exec"),
        groups=PARTNER, allowed={"nuc1"}, calls=calls,
    )
    response = client.delete(f"/tasks/{TASK_ID}")
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "WEBSOCKET_CLASS"
    assert "Sessions page" in response.json()["detail"]["message"]
    assert calls == [], "a refusal must never reach the cluster"


@pytest.mark.parametrize(
    "overrides,code",
    [
        ({"operation_id": None, "kind": "dashboard_action"}, "NO_OPERATION"),
        ({"status": "success"}, "ALREADY_FINISHED"),
        ({"status": "lost"}, "ALREADY_FINISHED"),
        ({"may_cancel": False}, "NOT_CANCELLABLE"),
    ],
)
def test_each_refusal_carries_its_code(monkeypatch, overrides: Dict[str, Any], code: str) -> None:
    calls: List[tuple] = []
    client = _client(monkeypatch, _row(**overrides), groups=PARTNER, allowed={"nuc1"}, calls=calls)
    response = client.delete(f"/tasks/{TASK_ID}")
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {"code": code, "message": response.json()["detail"]["message"]}
    assert calls == []


def test_a_live_cancel_reaches_the_cluster_with_a_socket_timeout_and_is_audited(monkeypatch) -> None:
    calls: List[tuple] = []
    audit = _Audit()
    client = _client(monkeypatch, _row(), groups=PARTNER, allowed={"nuc1"}, calls=calls, audit=audit)
    response = client.delete(f"/tasks/{TASK_ID}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True and body["task_id"] == TASK_ID and body["operation_id"] == "op-77"
    # One DELETE, addressed by index (never a positional path), with the per-attempt timeout
    # that frees the thread when the cluster goes silent.
    assert calls == [("op-77", (3, 8))]
    assert len(audit.records) == 1
    record = audit.records[0]
    assert record["action"] == "task.cancel"
    assert record["target_type"] == "task" and record["target_id"] == TASK_ID
    assert record["target_host"] == "nuc1" and record["target_project"] == "cust-a"
    assert record["details"] == {"task_id": TASK_ID, "operation_id": "op-77"}
    assert record.get("result", "success") == "success"


def test_an_operation_the_cluster_no_longer_holds_is_already_finished(monkeypatch) -> None:
    from pylxd.exceptions import NotFound

    gone = NotFound(SimpleNamespace(status_code=404, json=lambda: {"error": "not found"}))
    client = _client(monkeypatch, _row(), groups=PARTNER, allowed={"nuc1"}, raises=gone)
    response = client.delete(f"/tasks/{TASK_ID}")
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "ALREADY_FINISHED"


def test_a_dead_cluster_answers_503_not_500(monkeypatch) -> None:
    client = _client(monkeypatch, _row(), groups=PARTNER, allowed={"nuc1"}, reachable=False)
    response = client.delete(f"/tasks/{TASK_ID}")
    assert response.status_code == 503, response.text
    assert "nuc1" in response.json()["detail"]


def test_a_tenant_is_refused_before_any_load(monkeypatch) -> None:
    calls: List[tuple] = []
    client = _client(
        monkeypatch, _row(), groups=["role_tenant"], allowed={"nuc1"},
        acl={"nuc1": {("*", "*"): {"view"}}}, calls=calls,
    )
    response = client.delete(f"/tasks/{TASK_ID}")
    assert response.status_code == 403, response.text
    assert calls == []


def test_a_row_outside_the_callers_scope_reads_as_absent(monkeypatch) -> None:
    """404, never 403: an id must not confirm that another organisation's task exists."""
    client = _client(monkeypatch, _row(cluster_id="vdc-hetzner"), groups=PARTNER, allowed={"nuc1"})
    response = client.delete(f"/tasks/{TASK_ID}")
    assert response.status_code == 404, response.text
    absent = _client(monkeypatch, None, groups=PARTNER, allowed={"nuc1"})
    assert absent.delete(f"/tasks/{TASK_ID}").status_code == 404


def test_a_malformed_id_is_422(monkeypatch) -> None:
    client = _client(monkeypatch, _row(), groups=PARTNER, allowed={"nuc1"})
    assert client.delete("/tasks/not-a-uuid").status_code == 422
    assert client.get("/tasks/not-a-uuid").status_code == 422


def test_read_only_mode_refuses_the_mutation(monkeypatch) -> None:
    from app.core import dependencies

    settings = SimpleNamespace(read_only_mode=True)
    monkeypatch.setattr(dependencies, "get_settings", lambda: settings)
    calls: List[tuple] = []
    client = _client(monkeypatch, _row(), groups=PARTNER, allowed={"nuc1"}, calls=calls)
    response = client.delete(f"/tasks/{TASK_ID}")
    assert response.status_code == 403, response.text
    assert calls == []


def test_the_predicate_is_judged_under_the_request_context() -> None:
    """The scope override sets contextvars inside the dependency; a copied context proves the
    router's per-row judgement reads that copy and not the process default."""
    from app.core.task_scope import task_is_visible

    def judge() -> bool:
        org_scope._allowed_clusters_var.set({"nuc1"})
        org_scope._tenant_acl_var.set(None)
        return task_is_visible(_row(cluster_id="vdc-hetzner"))

    assert contextvars.copy_context().run(judge) is False
    assert task_is_visible(_row(cluster_id="vdc-hetzner")) is True
