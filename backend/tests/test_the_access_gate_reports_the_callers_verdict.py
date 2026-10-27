"""GET /me/access tells the dashboard whether to show the access notice, and agrees with enforcement.

Issue #17. The verdict for a Tenant is computed from the same three steps org_scope_dependency takes
for every scoped route, so "no cluster access yet" on the notice and an empty scope on the API are
one fact. The second half runs it against real rows: a member with an assigned policy on a bound
cluster, and a member of the same organization with nothing assigned.
"""

import asyncio
from typing import Optional

import pytest

from fastapi import Response
from sqlalchemy import JSON, MetaData

from app.core import org_scope
from app.core.auth import AuthenticatedUser
from app.core.org_scope import TenantPolicyGrant
from app.models.db_models import (
    Organization,
    OrganizationCluster,
    OrganizationMember,
    TenantAccessPolicy,
    TenantPolicyAssignment,
    TenantPolicyRule,
)
from app.routers import me
from tests._sqlite_session import sqlite_session


def _user(email: str, *groups: str) -> AuthenticatedUser:
    return AuthenticatedUser(user_id=email, email=email, groups=list(groups))


def _verdict(user: Optional[AuthenticatedUser], db=None):
    response = Response()
    verdict = asyncio.run(me.get_my_access_verdict(response=response, user=user, db=db))
    assert response.headers["Cache-Control"] == "no-store"
    return verdict


def test_no_role_is_reported_without_touching_the_database() -> None:
    verdict = _verdict(_user("new@example.test", "Everyone"), db=object())
    assert (verdict.has_role, verdict.role, verdict.blocked_reason) == (False, None, "no_role_assigned")
    assert verdict.email == "new@example.test"


@pytest.mark.parametrize("group,role", [("role_admin", "admin"), ("role_partner", "partner"), ("orcastra-admins", "admin")])
def test_admin_and_partner_are_never_blocked(group: str, role: str) -> None:
    verdict = _verdict(_user("op@example.test", group), db=object())
    assert (verdict.has_role, verdict.role, verdict.blocked_reason, verdict.tenant_access) == (True, role, None, None)


def test_dev_mode_reads_as_admin() -> None:
    verdict = _verdict(None)
    assert (verdict.role, verdict.blocked_reason) == ("admin", None)


def _tenant_verdict_and_scope(monkeypatch: pytest.MonkeyPatch, allowed, acl, grants):
    async def fake_allowed(_user, _db):
        return allowed

    async def fake_tenant_scope(_user, _db):
        return acl, grants, None

    async def fake_org_names(_user, _db):
        return []

    monkeypatch.setattr(org_scope, "get_user_allowed_clusters", fake_allowed)
    monkeypatch.setattr(org_scope, "get_tenant_scope", fake_tenant_scope)
    monkeypatch.setattr(org_scope, "_resolve_user_org_names", fake_org_names)
    user = _user("t@example.test", "role_tenant")

    async def enforced():
        await org_scope.org_scope_dependency(user=user, db=None)
        return org_scope.get_current_allowed_clusters()

    return _verdict(user), asyncio.run(enforced())


def _grant(cluster: str, instance: str = "*") -> TenantPolicyGrant:
    return TenantPolicyGrant(
        organization_id=1, policy_name="p", cluster_id=cluster, project_name="default",
        instance_name=instance, permissions=frozenset({"view"}),
    )


@pytest.mark.parametrize(
    "allowed,acl,grants,blocked",
    [
        ({"nuc1", "nuc2"}, {}, [], True),
        (set(), {}, [], True),
        ({"nuc1"}, {"nuc9": {("default", "*"): {"view"}}}, [_grant("nuc9")], True),
        ({"nuc1", "nuc2"}, {"nuc1": {("default", "*"): {"view"}}}, [_grant("nuc1"), _grant("nuc1", "web")], False),
    ],
)
def test_a_tenant_verdict_matches_the_enforced_scope(monkeypatch, allowed, acl, grants, blocked) -> None:
    verdict, scope = _tenant_verdict_and_scope(monkeypatch, allowed, acl, grants)
    assert scope is not None
    assert (verdict.blocked_reason == "no_tenant_access") is blocked
    assert (len(scope) == 0) is blocked
    assert verdict.tenant_access.total_clusters == len(scope)
    assert verdict.tenant_access.total_rules == len(grants)


@pytest.fixture
def world():
    db = sqlite_session(Organization, OrganizationMember, OrganizationCluster, TenantAccessPolicy, TenantPolicyAssignment)
    # note: SQLite has no JSONB, so only this copied DDL uses JSON; the ORM queries are unchanged.
    metadata = MetaData()
    Organization.__table__.to_metadata(metadata)
    TenantAccessPolicy.__table__.to_metadata(metadata)
    rules = TenantPolicyRule.__table__.to_metadata(metadata)
    rules.c.permissions.type = JSON()
    rules.create(db.sync.bind)

    org = Organization(name="QA gate", slug="qa-gate", created_by="partner@example.test")
    db.add(org)
    db.sync.flush()
    db.add(OrganizationCluster(organization_id=org.id, cluster_id="qa-cluster"))
    granted = OrganizationMember(organization_id=org.id, user_email="granted@example.test")
    waiting = OrganizationMember(organization_id=org.id, user_email="waiting@example.test")
    policy = TenantAccessPolicy(organization_id=org.id, name="qa-gate", created_by="partner@example.test")
    db.add_all([granted, waiting, policy])
    db.sync.flush()
    db.add(TenantPolicyRule(
        policy_id=policy.id, cluster_id="qa-cluster", project_name="default",
        instance_name="*", permissions=["view"],
    ))
    db.add(TenantPolicyAssignment(policy_id=policy.id, org_member_id=granted.id, assigned_by="partner@example.test"))
    db.sync.commit()
    try:
        yield db
    finally:
        db.sync.close()
        db.sync.bind.dispose()


def test_real_rows_a_member_with_nothing_assigned_is_blocked(world) -> None:
    verdict = _verdict(_user("waiting@example.test", "role_tenant"), world)
    assert verdict.blocked_reason == "no_tenant_access"
    assert (verdict.tenant_access.total_clusters, verdict.tenant_access.total_rules) == (0, 0)


def test_real_rows_a_member_with_a_policy_is_let_in(world) -> None:
    verdict = _verdict(_user("granted@example.test", "role_tenant"), world)
    assert verdict.blocked_reason is None
    assert (verdict.tenant_access.total_clusters, verdict.tenant_access.total_rules) == (1, 1)


def test_real_rows_a_stranger_with_the_tenant_role_is_blocked(world) -> None:
    verdict = _verdict(_user("stranger@example.test", "role_tenant"), world)
    assert verdict.blocked_reason == "no_tenant_access"
