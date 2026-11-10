"""The access verdict tells a tenant why their access is narrower, and when it ended.

The shell reads /me/access before it renders. It carries the term phase of the caller's own
grants: read-only projects (with their dates when OrcaHub sent them, inferred when it did not),
projects whose term ends within two weeks, and, for a tenant left with nothing, when that
access ended. It never names another tenant's project.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import Response

from app.core import org_scope
from app.core.auth import AuthenticatedUser
from app.core.org_scope import TenantPolicyGrant
from app.routers import me

NOW = datetime.now(timezone.utc)
FULL = frozenset({"view", "operate", "console", "snapshot"})


def _grant(project: str, permissions=FULL, **kwargs) -> TenantPolicyGrant:
    return TenantPolicyGrant(
        organization_id=1, policy_name=f"auto:{project}", cluster_id="nuc2", project_name=project,
        instance_name="*", permissions=frozenset(permissions), source=kwargs.pop("source", "orcahub"), **kwargs,
    )


def test_a_project_past_its_term_is_listed_read_only_with_its_dates() -> None:
    items = me._term_items([
        _grant("paid"),
        _grant("shop", {"view"}, term_ended_at=NOW - timedelta(days=1), access_ends_at=NOW + timedelta(days=29)),
    ])
    assert [(i.project_name, i.phase, i.inferred) for i in items] == [("shop", "read_only", False)]
    assert items[0].access_ends_at is not None


def test_a_downgrade_without_dates_is_read_only_by_inference() -> None:
    (item,) = me._term_items([_grant("shop", {"view", "console"})])
    assert item.phase == "read_only" and item.inferred and item.term_ended_at is None


def test_a_term_ending_soon_is_announced() -> None:
    (item,) = me._term_items([_grant("shop", term_ended_at=NOW + timedelta(days=5))])
    assert item.phase == "ending"


def test_a_project_still_operable_through_another_policy_is_not_read_only() -> None:
    items = me._term_items([
        _grant("shop", {"view"}, term_ended_at=NOW - timedelta(days=1)),
        _grant("shop", FULL, source="local"),
    ])
    assert items == []


def _tenant() -> AuthenticatedUser:
    return AuthenticatedUser(user_id="t", email="buyer@example.test", groups=["role_tenant"], roles=["role_tenant"])


async def test_a_tenant_whose_access_ended_is_told_when(monkeypatch) -> None:
    async def allowed(_user, _db):
        return {"nuc2"}

    async def scope(_user, _db):
        return {}, [], None

    async def ended(_user, _db):
        return NOW - timedelta(days=2)

    monkeypatch.setattr(org_scope, "get_user_allowed_clusters", allowed)
    monkeypatch.setattr(org_scope, "get_tenant_scope", scope)
    monkeypatch.setattr(me, "_latest_access_end", ended)

    verdict = await me.get_my_access_verdict(Response(), user=_tenant(), db=SimpleNamespace())

    assert verdict.blocked_reason == "no_tenant_access"
    assert verdict.tenant_access.phase == "ended"
    assert verdict.tenant_access.ended_at == NOW - timedelta(days=2)


async def test_a_tenant_with_a_read_only_project_keeps_access_and_says_so(monkeypatch) -> None:
    grant = _grant("shop", {"view"}, term_ended_at=NOW - timedelta(days=1))

    async def allowed(_user, _db):
        return {"nuc2"}

    async def scope(_user, _db):
        return {"nuc2": {("shop", "*"): {"view"}}}, [grant], None

    monkeypatch.setattr(org_scope, "get_user_allowed_clusters", allowed)
    monkeypatch.setattr(org_scope, "get_tenant_scope", scope)

    verdict = await me.get_my_access_verdict(Response(), user=_tenant(), db=SimpleNamespace())

    assert verdict.blocked_reason is None
    assert verdict.tenant_access.phase == "read_only"
    assert [i.project_name for i in verdict.tenant_access.items] == ["shop"]
