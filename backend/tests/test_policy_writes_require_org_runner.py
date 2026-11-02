"""A global Partner may read a member organization, but only its runners may grant access."""

import asyncio
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles

from app.core.auth import AuthenticatedUser, get_current_user
from app.core.database import get_db
from app.core.dependencies import check_read_only_mode
from app.core.org_scope import get_tenant_policy_grants, org_scope_dependency
from app.models.db_models import (
    Organization,
    OrganizationCluster,
    OrganizationMember,
    TenantAccessPolicy,
    TenantPolicyAssignment,
    TenantPolicyRule,
)
from app.routers import tenant_access
from tests._sqlite_session import sqlite_session


@compiles(JSONB, "sqlite")
def _sqlite_jsonb(_type, _compiler, **_kwargs):
    return "JSON"


ORG = 1
READ_POLICY = 1
EXPANSION_POLICY = 2
RECIPIENT = "recipient@example.test"
MEMBER = "member@example.test"
CLUSTER = "cluster-one"


@pytest.fixture
def world(monkeypatch):
    db = sqlite_session(
        Organization, OrganizationMember, OrganizationCluster,
        TenantAccessPolicy, TenantPolicyRule, TenantPolicyAssignment,
    )
    db.add(Organization(name="Acme", slug="acme", created_by="Creator@Example.TEST", is_active=True))
    db.add(OrganizationCluster(organization_id=ORG, cluster_id=CLUSTER))
    db.add_all([
        OrganizationMember(organization_id=ORG, user_email="Member@Example.TEST", role="member"),
        OrganizationMember(organization_id=ORG, user_email="Runner@Example.TEST", role="partner"),
        OrganizationMember(organization_id=ORG, user_email=RECIPIENT, role="tenant"),
    ])
    db.add_all([
        TenantAccessPolicy(organization_id=ORG, name="Read", created_by="Creator@Example.TEST"),
        TenantAccessPolicy(organization_id=ORG, name="Expand", created_by="Creator@Example.TEST"),
    ])
    asyncio.run(db.commit())
    db.add_all([
        TenantPolicyRule(
            policy_id=READ_POLICY, cluster_id=CLUSTER, project_name="default",
            instance_name="*", permissions=["view"],
        ),
        TenantPolicyRule(
            policy_id=EXPANSION_POLICY, cluster_id=CLUSTER, project_name="default",
            instance_name="*", permissions=["operate"],
        ),
        TenantPolicyAssignment(policy_id=READ_POLICY, org_member_id=3, assigned_by="Creator@Example.TEST"),
        TenantPolicyAssignment(policy_id=READ_POLICY, org_member_id=1, assigned_by="Creator@Example.TEST"),
    ])
    asyncio.run(db.commit())

    @asynccontextmanager
    async def no_migration_fence(_rules, _db, policy_id=None):
        yield

    monkeypatch.setattr(tenant_access, "_guard_rules_mutation", no_migration_fence)
    monkeypatch.setattr(tenant_access, "_invalidate_acl_caches", lambda: None)
    yield db
    db.sync.close()


def client_for(db, email, role="role_partner"):
    app = FastAPI()
    app.include_router(tenant_access.router, prefix="/api/v1")
    user = AuthenticatedUser(user_id=email or "no-email", email=email, groups=[role])
    app.dependency_overrides[get_current_user] = lambda: user

    async def provide_db():
        try:
            yield db
        finally:
            await db.rollback()
            db.sync.expire_all()

    app.dependency_overrides[get_db] = provide_db
    app.dependency_overrides[org_scope_dependency] = lambda: None
    app.dependency_overrides[check_read_only_mode] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def rows(db):
    async def read():
        policies = (await db.execute(select(
            TenantAccessPolicy.id, TenantAccessPolicy.name, TenantAccessPolicy.description,
        ).order_by(TenantAccessPolicy.id))).all()
        rules = (await db.execute(select(
            TenantPolicyRule.id, TenantPolicyRule.policy_id, TenantPolicyRule.permissions,
        ).order_by(TenantPolicyRule.id))).all()
        assignments = (await db.execute(select(
            TenantPolicyAssignment.id, TenantPolicyAssignment.policy_id,
            TenantPolicyAssignment.org_member_id,
        ).order_by(TenantPolicyAssignment.id))).all()
        return policies, rules, assignments

    return asyncio.run(read())


def grants(db):
    user = AuthenticatedUser(user_id=RECIPIENT, email=RECIPIENT, groups=["role_tenant"])
    return asyncio.run(get_tenant_policy_grants(user, db))


async def set_member_role(db, role):
    member = await db.get(OrganizationMember, 1)
    member.role = role
    await db.commit()


@pytest.mark.parametrize("member_role", ["member", "tenant"])
def test_nonrunner_partner_cannot_change_policy_or_another_tenants_acl(world, member_role):
    asyncio.run(set_member_role(world, member_role))
    client = client_for(world, MEMBER)
    before_rows = rows(world)
    before_grants = grants(world)
    assert [(grant.policy_name, grant.permissions) for grant in before_grants] == [
        ("Read", frozenset({"view"})),
    ]

    requests = [
        ("post", "/policies", {"name": "Admin", "organization_id": ORG, "rules": []}),
        ("put", f"/policies/{READ_POLICY}", {"name": "Changed"}),
        ("delete", f"/policies/{READ_POLICY}", None),
        ("post", "/assignments", {
            "policy_id": EXPANSION_POLICY, "organization_id": ORG, "user_email": RECIPIENT,
        }),
        ("delete", "/assignments/1", None),
    ]
    for method, route, body in requests:
        response = client.request(method, f"/api/v1/tenant-access{route}", json=body)
        assert response.status_code == 403, (method, route, response.text)
        assert rows(world) == before_rows, (method, route)
        assert grants(world) == before_grants, (method, route)
        assert not world.sync.new and not world.sync.dirty and not world.sync.deleted


@pytest.mark.parametrize("member_role", ["member", "tenant"])
def test_nonrunner_partner_can_revoke_only_own_assignment(world, member_role):
    asyncio.run(set_member_role(world, member_role))
    client = client_for(world, MEMBER)
    before_grants = grants(world)
    response = client.delete("/api/v1/tenant-access/assignments/2")
    assert response.status_code == 204, response.text
    assert [assignment[0] for assignment in rows(world)[2]] == [1]
    assert grants(world) == before_grants


@pytest.mark.parametrize("email,role", [
    ("admin@example.test", "role_admin"),
    ("creator@example.test", "role_partner"),
    ("runner@example.test", "role_partner"),
])
def test_admin_creator_and_partner_runner_can_assign(world, email, role):
    client = client_for(world, email, role)
    response = client.post("/api/v1/tenant-access/assignments", json={
        "policy_id": EXPANSION_POLICY, "organization_id": ORG, "user_email": RECIPIENT,
    })
    assert response.status_code == 201, response.text
    assert response.json()["assigned_by"] == email
    assert {(grant.policy_name, tuple(sorted(grant.permissions))) for grant in grants(world)} == {
        ("Read", ("view",)), ("Expand", ("operate",)),
    }


def test_global_tenant_cannot_self_revoke(world):
    client = client_for(world, RECIPIENT, "role_tenant")
    before = rows(world)
    response = client.delete("/api/v1/tenant-access/assignments/1")
    assert response.status_code == 403, response.text
    assert rows(world) == before


def test_partner_outside_organization_cannot_assign(world):
    client = client_for(world, "outsider@example.test")
    before = rows(world)
    before_grants = grants(world)
    response = client.post("/api/v1/tenant-access/assignments", json={
        "policy_id": EXPANSION_POLICY, "organization_id": ORG, "user_email": RECIPIENT,
    })
    assert response.status_code == 403, response.text
    assert rows(world) == before
    assert grants(world) == before_grants


def test_partner_without_email_cannot_use_an_empty_membership(world):
    client = client_for(world, None)
    before = rows(world)
    response = client.post("/api/v1/tenant-access/assignments", json={
        "policy_id": EXPANSION_POLICY, "organization_id": ORG, "user_email": RECIPIENT,
    })
    assert response.status_code == 403, response.text
    assert rows(world) == before
