"""The task and cluster-log reads never hand a caller a row outside their scope.

Two gates, both driven through the real routers: the SQL the page is built from carries the
caller's cluster set (asserted on the compiled statement the fake session recorded), and the
per-row judgement drops anything the SQL would not have returned (the fake answers rows the
predicate must refuse, which is exactly what a wrongly built WHERE clause would do). A
foreign `cluster_id` filter answers 403 from `verify_cluster_access`, `actor=me` without an
email claim answers 403, and a malformed status answers 422.
"""

import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from _journal_fakes import FakeDB, FakeResult  # noqa: E402
from app.core import org_scope  # noqa: E402
from app.core.auth import AuthenticatedUser  # noqa: E402
from app.core.database import get_db  # noqa: E402
from app.core.rbac import get_current_user  # noqa: E402
from app.routers import cluster_log as log_router  # noqa: E402
from app.routers import tasks as tasks_router  # noqa: E402
from app.services.lxd.lxd_service import get_lxd_service  # noqa: E402

MINE = "8d1b2c1e-0000-4000-8000-000000000001"
THEIRS = "8d1b2c1e-0000-4000-8000-000000000002"
NETWORK = "8d1b2c1e-0000-4000-8000-000000000003"
STAMP = "2026-09-19T00:00:00+00:00"


def _task(row_id: str, **overrides: Any) -> Dict[str, Any]:
    row = {
        "id": row_id, "seq": 1, "kind": "lxd_operation", "cluster_id": "nuc1", "project": "cust-a",
        "resource_type": "instance", "resource_name": "web-01", "operation_id": "op-1",
        "operation_class": "task", "action": "instance.start", "description": "Starting instance",
        "status": "running", "status_code": 103, "progress": None, "error": None, "may_cancel": True,
        "actor_email": "ops@example.test", "actor_role": "partner", "actor_kind": "user",
        "api_key_id": None, "source": "dashboard", "request_id": None, "target_organization_id": 6,
        "target_organization_slug": "sivali", "metadata": {"location": "m1"}, "lxd_created_at": None,
        "lxd_updated_at": None, "started_at": None, "finished_at": None, "created_at": STAMP,
        "updated_at": STAMP,
    }
    row.update(overrides)
    return row


def _log(row_id: int, **overrides: Any) -> Dict[str, Any]:
    row = {
        "id": row_id, "ts": STAMP, "kind": "audit", "severity": "info", "cluster_id": "nuc1",
        "project": "cust-a", "resource_type": "instance", "resource_name": "web-01",
        "action": "instance.start", "message": "Started web-01", "actor_email": "ops@example.test",
        "actor_role": "partner", "actor_kind": "user", "api_key_id": None, "result": "success",
        "request_id": None, "event_id": None, "task_id": None, "target_organization_id": 6,
        "details": {"operation_id": "op-1", "config_keys": ["limits.cpu"]},
    }
    row.update(overrides)
    return row


TASK_ROWS = [
    _task(MINE),
    _task(THEIRS, cluster_id="vdc-hetzner"),
    _task(NETWORK, resource_type="network", resource_name="br0"),
]
LOG_ROWS = [
    _log(1),
    _log(2, cluster_id="vdc-hetzner"),
    _log(3, resource_type="network", resource_name="br0", kind="lifecycle"),
    _log(4, cluster_id=None, actor_email="ops@example.test", action="api_key.create"),
    _log(5, cluster_id=None, actor_email="other@example.test", action="api_key.create"),
]


class _Recorder:
    def __init__(self) -> None:
        self.dbs: List[FakeDB] = []

    @property
    def statements(self) -> List[str]:
        return [sql for db in self.dbs for sql in db.statements]


def _client(
    *,
    groups: List[str],
    allowed: Optional[set],
    acl: Optional[dict] = None,
    email: Optional[str] = "ops@example.test",
    task_rows: Optional[List[Dict[str, Any]]] = None,
    log_rows: Optional[List[Dict[str, Any]]] = None,
) -> tuple:
    recorder = _Recorder()
    tasks = TASK_ROWS if task_rows is None else task_rows
    logs = LOG_ROWS if log_rows is None else log_rows

    def respond(sql: str, _params: Any) -> Optional[FakeResult]:
        if "pg_locks" in sql:
            return FakeResult(scalar=True)
        if "task_follower_clusters" in sql:
            return FakeResult(rows=[{"cluster_id": "nuc1", "state": "connected"},
                                    {"cluster_id": "vdc-hetzner", "state": "disconnected"}])
        if "count(" in sql:
            return FakeResult(scalar=len(tasks) if "task_journal" in sql else len(logs))
        if "FROM task_journal" in sql:
            return FakeResult(rows=list(tasks))
        if "FROM cluster_log" in sql:
            return FakeResult(rows=list(logs))
        return FakeResult(rows=[])

    async def _db():
        db = FakeDB(respond)
        recorder.dbs.append(db)
        yield db

    user = AuthenticatedUser(
        user_id=f"uid-{'-'.join(groups)}-{email or 'noemail'}", email=email, groups=groups
    )

    async def _scope() -> None:
        org_scope._allowed_clusters_var.set(allowed)
        org_scope._tenant_acl_var.set(acl)
        org_scope._visible_projects_var.set(None)

    app = FastAPI()
    app.include_router(tasks_router.router)
    app.include_router(log_router.router)
    app.dependency_overrides[get_lxd_service] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[org_scope.org_scope_dependency] = _scope
    app.dependency_overrides[get_db] = _db
    return TestClient(app, raise_server_exceptions=False), recorder


TENANT = dict(groups=["role_tenant"], allowed={"nuc1"}, acl={"nuc1": {("cust-a", "*"): {"view"}}})
PARTNER = dict(groups=["role_partner"], allowed={"nuc1"})
ADMIN = dict(groups=["role_admin"], allowed=None)


def test_a_tenant_never_receives_a_network_row_nor_a_foreign_cluster_row() -> None:
    client, recorder = _client(**TENANT)
    response = client.get("/tasks")
    assert response.status_code == 200, response.text
    body = response.json()
    assert [t["id"] for t in body["tasks"]] == [MINE]
    # Tenant field policy on the wire.
    assert body["tasks"][0]["target_organization_id"] is None
    assert body["tasks"][0]["location"] is None
    assert body["follower_active"] is True
    assert body["disconnected_clusters"] == []
    # The page statement itself carried the cluster set and the ACL predicate.
    page = next(s for s in recorder.statements if "FROM task_journal" in s and "count(" not in s)
    assert "task_journal.cluster_id IN" in page
    assert "task_journal.resource_type = " in page


def test_a_partner_receives_the_network_row_but_not_the_foreign_cluster() -> None:
    client, _recorder = _client(**PARTNER)
    body = client.get("/tasks").json()
    assert sorted(t["id"] for t in body["tasks"]) == sorted([MINE, NETWORK])
    assert body["tasks"][0]["location"] == "m1"
    assert body["tasks"][0]["target_organization_id"] is None
    assert body["disconnected_clusters"] == []


def test_an_admin_receives_every_row_and_the_organisation() -> None:
    client, recorder = _client(**ADMIN)
    body = client.get("/tasks").json()
    assert sorted(t["id"] for t in body["tasks"]) == sorted([MINE, THEIRS, NETWORK])
    assert body["tasks"][0]["target_organization_id"] == 6
    assert body["disconnected_clusters"] == ["vdc-hetzner"]
    page = next(s for s in recorder.statements if "FROM task_journal" in s and "count(" not in s)
    assert "cluster_id IN" not in page


@pytest.mark.parametrize("path", ["/tasks", "/cluster-log"])
def test_a_foreign_cluster_filter_is_403(path: str) -> None:
    client, recorder = _client(**PARTNER)
    response = client.get(path, params={"cluster_id": "vdc-hetzner"})
    assert response.status_code == 403, response.text
    assert recorder.statements == [], "refused before any query"
    assert client.get(path, params={"cluster_id": "nuc1"}).status_code == 200


@pytest.mark.parametrize("path", ["/tasks", "/cluster-log"])
def test_actor_me_without_an_email_claim_is_403(path: str) -> None:
    client, _recorder = _client(**dict(PARTNER, email=None))
    assert client.get(path, params={"actor": "me"}).status_code == 403
    with_email, recorder = _client(**PARTNER)
    assert with_email.get(path, params={"actor": "me"}).status_code == 200
    page = next(s for s in recorder.statements if "count(" not in s and ("task_journal" in s or "cluster_log" in s))
    assert "actor_email = " in page


def test_a_bad_status_or_kind_is_422() -> None:
    client, recorder = _client(**PARTNER)
    assert client.get("/tasks", params={"status": "running,bogus"}).status_code == 422
    assert client.get("/tasks", params={"kind": "sideways"}).status_code == 422
    assert client.get("/cluster-log", params={"severity": "loud"}).status_code == 422
    assert client.get("/tasks", params={"limit": 501}).status_code == 422
    assert client.get("/tasks/queue", params={"limit": 501}).status_code == 422
    assert client.get("/tasks/queue", params={"limit": 0}).status_code == 422
    assert client.get("/tasks/queue", params={"offset": -1}).status_code == 422
    assert recorder.statements == []
    assert client.get("/tasks", params={"status": "running, failure"}).status_code == 200


def test_limit_zero_answers_the_counts_only() -> None:
    client, recorder = _client(**PARTNER)
    body = client.get("/tasks", params={"limit": 0}).json()
    assert body["tasks"] == [] and body["total"] == 3 and body["limit"] == 0
    assert body["has_more"] is True
    assert not any("LIMIT" in s for s in recorder.statements if "task_journal" in s)


def test_a_foreign_task_detail_reads_as_absent() -> None:
    client, _recorder = _client(**PARTNER, task_rows=[_task(THEIRS, cluster_id="vdc-hetzner")])
    assert client.get(f"/tasks/{THEIRS}").status_code == 404
    mine, _recorder = _client(**PARTNER, task_rows=[_task(MINE)], log_rows=[_log(1, task_id=MINE)])
    response = mine.get(f"/tasks/{MINE}")
    assert response.status_code == 200, response.text
    assert response.json()["task"]["id"] == MINE
    assert [line["id"] for line in response.json()["log"]] == [1]


def test_the_cluster_log_keeps_hostless_rows_to_admin_and_their_own_actor() -> None:
    partner, recorder = _client(**PARTNER)
    body = partner.get("/cluster-log").json()
    assert sorted(e["id"] for e in body["entries"]) == [1, 3, 4]
    page = next(s for s in recorder.statements if "FROM cluster_log" in s and "count(" not in s)
    assert "cluster_log.cluster_id IS NULL AND cluster_log.actor_email = " in page
    assert "cluster_log.ts >= " in page, "the window is always bounded"

    tenant, _recorder = _client(**TENANT)
    body = tenant.get("/cluster-log").json()
    assert sorted(e["id"] for e in body["entries"]) == [1, 4]
    assert body["entries"][0]["details"] == {"operation_id": "op-1"}

    admin, _recorder = _client(**ADMIN)
    body = admin.get("/cluster-log").json()
    assert sorted(e["id"] for e in body["entries"]) == [1, 2, 3, 4, 5]


def test_the_queue_groups_open_rows_per_visible_cluster() -> None:
    client, _recorder = _client(**PARTNER, task_rows=[_task(MINE), _task(THEIRS, cluster_id="vdc-hetzner"),
                                                     _task(NETWORK, status="pending")])
    response = client.get("/tasks/queue")
    assert response.status_code == 200, response.text
    body = response.json()
    assert [c["cluster_id"] for c in body["clusters"]] == ["nuc1"]
    cluster = body["clusters"][0]
    assert cluster["state"] == "connected"
    assert [t["id"] for t in cluster["running"]] == [MINE]
    assert [t["id"] for t in cluster["pending"]] == [NETWORK]
    assert cluster["due_schedules"] == []


def test_a_naive_since_stamp_is_read_as_utc() -> None:
    from datetime import timezone

    from app.routers.tasks import utc_bound

    client, recorder = _client(**PARTNER)
    assert client.get("/tasks", params={"since": "2026-09-19T00:00:00"}).status_code == 200
    assert client.get("/cluster-log", params={"since": "2026-09-19T00:00:00"}).status_code == 200
    bound = utc_bound(datetime.fromisoformat("2026-09-19T00:00:00"))
    assert bound.tzinfo is timezone.utc
    aware = datetime.fromisoformat("2026-09-19T00:00:00+07:00")
    assert utc_bound(aware) is aware
    assert any("updated_at >= " in s for s in recorder.statements)
