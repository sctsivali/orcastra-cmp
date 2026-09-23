"""A tenant reads only its own access policies and assignments, never the organization's matrix.

`GET /tenant-access/policies`, `GET /tenant-access/assignments` and `GET /tenant-access/policies/{id}`
were scoped by organization alone. That handed every tenant the full access matrix of each of
their organizations and, on the assignments list, every other member's email. A partner and an
admin keep the org-wide view they administer with.

The list filters are checked on the compiled SQL, because the fake session cannot run a query:
the caller's own email appears once more for a tenant than for a partner, which is exactly the
added filter.
"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.core.auth import AuthenticatedUser
from app.routers import tenant_access


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> "_Result":
        return self

    def unique(self) -> "_Result":
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def scalar_one_or_none(self) -> Optional[Any]:
        return self._rows[0] if self._rows else None


class _CapturingSession:
    def __init__(self, answers: Optional[list[list[Any]]] = None) -> None:
        self.statements: list[Any] = []
        self._answers = list(answers or [])

    async def execute(self, stmt: Any) -> _Result:
        self.statements.append(stmt)
        return _Result(self._answers.pop(0) if self._answers else [])

    def sql(self, index: int = 0) -> str:
        return str(
            self.statements[index].compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )


def _user(role: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=f"{role}-tenant-access-probe", email=f"{role}@example.test", groups=[f"role_{role}"]
    )


@pytest.fixture(autouse=True)
def org_access_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_verify(_user: Any, _org_id: int, _db: Any, require_write: bool = False) -> Any:
        return SimpleNamespace(id=_org_id)

    monkeypatch.setattr(tenant_access, "_verify_org_access", fake_verify)


def _own_email_count(route: str, role: str, organization_id: Optional[int]) -> int:
    session = _CapturingSession()
    user = _user(role)
    handler = getattr(tenant_access, route)
    asyncio.run(handler(organization_id=organization_id, user=user, db=session))
    return session.sql().count(user.email)


@pytest.mark.parametrize("route", ["list_policies", "list_assignments"])
@pytest.mark.parametrize("organization_id,org_scope_mentions", [(7, 0), (None, 2)])
def test_a_tenant_list_adds_exactly_its_own_filter(
    route: str, organization_id: Optional[int], org_scope_mentions: int
) -> None:
    # note: without an organization id the membership and creator subqueries name the caller
    # too, so the partner baseline is two mentions there and none with an explicit organization.
    assert _own_email_count(route, "partner", organization_id) == org_scope_mentions
    assert _own_email_count(route, "tenant", organization_id) == org_scope_mentions + 1


def test_the_tenant_policy_filter_goes_through_its_assignments() -> None:
    session = _CapturingSession()
    asyncio.run(tenant_access.list_policies(organization_id=7, user=_user("tenant"), db=session))
    sql = session.sql()
    assert "EXISTS" in sql and "tenant_policy_assignments" in sql


def _policy(assignee_member_ids: list[int]) -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=3,
        organization_id=7,
        name="oak_test",
        description=None,
        is_default=False,
        source="local",
        external_id=None,
        created_by="partner@example.test",
        created_at=now,
        updated_at=now,
        rules=[],
        assignments=[SimpleNamespace(org_member_id=m) for m in assignee_member_ids],
    )


def _get_policy(role: str, assignees: list[int], own_member_ids: list[int]) -> Any:
    session = _CapturingSession([[_policy(assignees)], own_member_ids])
    return asyncio.run(tenant_access.get_policy(policy_id=3, user=_user(role), db=session))


def test_a_tenant_cannot_read_a_policy_assigned_to_someone_else() -> None:
    with pytest.raises(HTTPException) as raised:
        _get_policy("tenant", assignees=[99], own_member_ids=[41])
    assert raised.value.status_code == 404
    assert raised.value.detail == "Policy not found"


def test_a_tenant_reads_a_policy_assigned_to_them() -> None:
    assert _get_policy("tenant", assignees=[99, 41], own_member_ids=[41]).name == "oak_test"


@pytest.mark.parametrize("role", ["partner", "admin"])
def test_a_partner_and_an_admin_read_any_policy_of_their_organization(role: str) -> None:
    assert _get_policy(role, assignees=[], own_member_ids=[]).name == "oak_test"
