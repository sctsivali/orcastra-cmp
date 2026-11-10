"""The tenant grant query enforces the paid term on the policy, whatever OrcaHub did or did not send.

Otherwise a buyer whose revoke never arrived would keep the project, and the cluster it sits on,
for as long as the policy lived. The grant query narrows a policy past its term to viewing and
drops a policy past its access end; these tests run the real statement.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core import org_scope
from app.core.auth import AuthenticatedUser

HOST = "nuc2"
EMAIL = "buyer@example.test"
NOW = datetime.now(timezone.utc)
FULL = '["view", "operate", "console", "snapshot", "file_transfer"]'


class _Adapter:
    def __init__(self, session: Session):
        self._session = session

    async def execute(self, statement):
        return self._session.execute(statement)


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S.%f")


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        for ddl in (
            "CREATE TABLE organizations (id INTEGER PRIMARY KEY, is_active BOOLEAN NOT NULL, source TEXT NOT NULL DEFAULT 'local')",
            "CREATE TABLE organization_members (id INTEGER PRIMARY KEY, organization_id INTEGER, user_email TEXT)",
            "CREATE TABLE organization_clusters (id INTEGER PRIMARY KEY, organization_id INTEGER, cluster_id TEXT, registered_by TEXT)",
            "CREATE TABLE tenant_access_policies (id INTEGER PRIMARY KEY, organization_id INTEGER, name TEXT, "
            "source TEXT NOT NULL, term_ended_at DATETIME, access_ends_at DATETIME)",
            "CREATE TABLE tenant_policy_assignments (id INTEGER PRIMARY KEY, policy_id INTEGER, org_member_id INTEGER, "
            "expires_at DATETIME)",
            "CREATE TABLE tenant_policy_rules (id INTEGER PRIMARY KEY, policy_id INTEGER, cluster_id TEXT, "
            "project_name TEXT, instance_name TEXT, permissions TEXT, created_at DATETIME)",
        ):
            conn.execute(text(ddl))
        conn.execute(text("INSERT INTO organizations (id, is_active) VALUES (1, 1)"))
        conn.execute(text("INSERT INTO organization_members VALUES (11, 1, :e)"), {"e": EMAIL})
        conn.execute(text("INSERT INTO organization_clusters (id, organization_id, cluster_id) VALUES (21, 1, :h)"), {"h": HOST})
        policies = [
            (31, "auto:paid", "orcahub", None, None),
            (32, "auto:retention", "orcahub", _iso(NOW - timedelta(days=2)), _iso(NOW + timedelta(days=28))),
            (33, "auto:gone", "orcahub", _iso(NOW - timedelta(days=40)), _iso(NOW - timedelta(minutes=1))),
            (34, "local:dated", "local", _iso(NOW - timedelta(days=1)), None),
        ]
        for pid, name, source, term, ends in policies:
            conn.execute(
                text("INSERT INTO tenant_access_policies VALUES (:i, 1, :n, :s, :t, :a)"),
                {"i": pid, "n": name, "s": source, "t": term, "a": ends},
            )
            conn.execute(text("INSERT INTO tenant_policy_assignments VALUES (:i, :i, 11, NULL)"), {"i": pid})
            conn.execute(
                text("INSERT INTO tenant_policy_rules VALUES (:i, :i, :h, :p, '*', :perm, '2026-01-01')"),
                {"i": pid, "h": HOST, "p": name.split(":")[1], "perm": FULL},
            )
    return _Adapter(Session(engine))


def _tenant() -> AuthenticatedUser:
    return AuthenticatedUser(user_id="buyer", email=EMAIL, groups=["role_tenant"], roles=["role_tenant"])


def _grants(db) -> dict:
    return {g.project_name: g for g in asyncio.run(org_scope.get_tenant_policy_grants(_tenant(), db))}


def test_a_paid_policy_keeps_everything(db) -> None:
    paid = _grants(db)["paid"]
    assert "operate" in paid.permissions and not paid.read_only


def test_a_policy_past_its_term_grants_viewing_only(db) -> None:
    retention = _grants(db)["retention"]
    assert retention.permissions == {"view"}
    assert retention.read_only and retention.access_ends_at is not None


def test_a_policy_past_its_access_end_grants_nothing(db) -> None:
    assert "gone" not in _grants(db)


def test_any_policy_with_a_passed_term_is_narrowed_not_only_orcahubs(db) -> None:
    assert _grants(db)["dated"].permissions == {"view"}


def test_the_switch_stops_enforcing_but_keeps_the_dates(db, monkeypatch) -> None:
    monkeypatch.setattr(
        org_scope, "get_settings", lambda: SimpleNamespace(tenant_term_enforcement_enabled=False)
    )
    grants = _grants(db)
    assert "gone" in grants
    assert "operate" in grants["retention"].permissions
    assert grants["retention"].term_ended_at is not None
