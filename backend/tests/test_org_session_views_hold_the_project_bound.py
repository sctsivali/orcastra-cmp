"""The Sessions page's org scope shows only sessions on instances the caller may view.

`GET /console/sessions/active?scope=org` and `GET /console/sessions/audit?scope=org` filtered by
the caller's cluster set alone. Every row names an instance, its project and a user's email, so
on a cluster bound to two organizations a Partner, or a tenant holding one rule there, read the
other organization's users, instances and projects for live and past console and terminal
sessions. Rows are now held to the project bound and, for a tenant, to `view` on the instance.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from fastapi import Response
from sqlalchemy.dialects import postgresql

from app.core.session_scope import audit_scope_clause
from app.models.db_models import SessionAuditLog
from app.routers import console
from app.services.session_tracker import SessionProtocol, get_session_tracker
from tests.instance_access_fakes import CLUSTER, partner, scoped, tenant
from tests.session_fakes import install_fake_registry

FOREIGN = "other-db"
USER = SimpleNamespace(email="partner@example.test", user_id="p1", groups=["role_partner"])
ROWS = [("s-default", "default", "web01"), ("s-own", "own-web", "web01"),
        ("s-own-db", "own-web", "db01"), ("s-foreign", FOREIGN, "web01")]


def _register_all() -> None:
    install_fake_registry()
    tracker = get_session_tracker()
    for session_id, project, instance in ROWS:
        tracker.register(
            session_id=session_id, instance_name=instance, host=CLUSTER, project=project,
            protocol=SessionProtocol.TERMINAL, user_id="u", user_email=f"{session_id}@example.test",
        )


async def _active() -> list[str]:
    rows = await console.list_active_tracked_sessions(
        response=Response(), instance_name=None, host=None, scope="org", _rbac=USER
    )
    return sorted(row["session_id"] for row in rows)


async def test_org_active_sessions_hide_another_organizations_project() -> None:
    _register_all()
    with partner({"default", "own-web"}):
        assert await _active() == ["s-default", "s-own", "s-own-db"]


async def test_org_active_sessions_hold_a_tenant_to_its_acl() -> None:
    _register_all()
    with tenant({("own-web", "web01"): {"view"}}):
        assert await _active() == ["s-own"]


async def test_org_active_sessions_are_whole_for_an_unbounded_caller() -> None:
    _register_all()
    with scoped(allowed={CLUSTER}):
        assert await _active() == sorted(r[0] for r in ROWS)


def _sql(clause: Any) -> str:
    return str(clause.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_the_audit_clause_bounds_the_shared_cluster_only() -> None:
    with scoped(allowed={CLUSTER, "cluster-b"}, bound={CLUSTER: {"default", "own-web"}}):
        sql = _sql(audit_scope_clause(SessionAuditLog, {CLUSTER, "cluster-b"}))
    assert "session_audit_logs.host = 'cluster-a' AND coalesce(" in sql
    assert "IN ('default', 'own-web')" in sql
    assert "session_audit_logs.host = 'cluster-b'" in sql
    assert FOREIGN not in sql


def test_an_empty_cluster_set_matches_no_audit_row() -> None:
    with scoped(allowed=set()):
        assert _sql(audit_scope_clause(SessionAuditLog, set())) == "false"


class _Db:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.statements: list[Any] = []

    async def __aenter__(self) -> "_Db":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def execute(self, statement: Any) -> Any:
        self.statements.append(statement)
        rows = self.rows
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))


def _audit_row(session_id: str, project: str, instance: str) -> Any:
    return SimpleNamespace(
        id=1, session_id=session_id, event_type="connect", instance_name=instance, host=CLUSTER,
        project=project, protocol="websocket-terminal", user_id="u",
        user_email=f"{session_id}@example.test", user_role="tenant", duration_seconds=None,
        download_bytes=0, upload_bytes=0, target_organization_id=None, target_org_slug=None,
        break_glass_grant_id=None, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


async def _audit(monkeypatch, db: _Db) -> list[str]:
    monkeypatch.setattr(console, "async_session_maker", lambda: db)
    rows = await console.query_session_audit_logs(
        instance_name=None, event_type=None, scope="org", limit=100, _rbac=USER
    )
    return sorted(row["session_id"] for row in rows)


async def test_org_audit_bounds_the_query_for_a_partner(monkeypatch) -> None:
    db = _Db([])
    with partner({"default", "own-web"}):
        await _audit(monkeypatch, db)
    sql = _sql(db.statements[0])
    assert "IN ('default', 'own-web')" in sql


async def test_org_audit_holds_a_tenant_to_its_acl(monkeypatch) -> None:
    db = _Db([_audit_row(*row) for row in ROWS])
    with tenant({("own-web", "web01"): {"view"}}):
        assert await _audit(monkeypatch, db) == ["s-own"]
