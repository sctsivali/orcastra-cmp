"""Policy details must obey the same Tenant assignment boundary as policy discovery."""

import asyncio
from dataclasses import dataclass

import pytest
from fastapi import HTTPException
from sqlalchemy import JSON, MetaData, select

from app.core.auth import AuthenticatedUser
from app.models.db_models import (
    Organization,
    OrganizationCluster,
    OrganizationMember,
    TenantAccessPolicy,
    TenantPolicyAssignment,
    TenantPolicyRule,
)
from app.routers import tenant_access
from tests._sqlite_session import AsyncSessionFacade, sqlite_session


@dataclass
class PolicyWorld:
    db: AsyncSessionFacade
    policies: dict[str, TenantAccessPolicy]
    members: dict[str, OrganizationMember]
    assignments: dict[str, TenantPolicyAssignment]


def actor(email: str, *roles: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=f"{email}:{','.join(roles)}",
        email=email,
        groups=[f"role_{role}" for role in roles] or ["role_tenant"],
    )


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch):
    db = sqlite_session(
        Organization, OrganizationMember, OrganizationCluster,
        TenantAccessPolicy, TenantPolicyAssignment,
    )
    # Only this copied SQLite DDL substitutes JSON for PostgreSQL JSONB. ORM queries and the
    # model metadata keep their original columns and authorization predicates.
    metadata = MetaData()
    Organization.__table__.to_metadata(metadata)
    TenantAccessPolicy.__table__.to_metadata(metadata)
    rule_table = TenantPolicyRule.__table__.to_metadata(metadata)
    rule_table.c.permissions.type = JSON()
    rule_table.create(db.sync.bind)

    org = Organization(name="QA primary", slug="qa-primary", created_by="partner@example.test")
    neighbor = Organization(name="QA neighbor", slug="qa-neighbor", created_by="neighbor-partner@example.test")
    db.add_all([org, neighbor])
    db.sync.flush()
    members = {
        name: OrganizationMember(organization_id=owner.id, user_email=email)
        for name, owner, email in (
            ("own", org, "own@example.test"),
            ("peer", org, "peer@example.test"),
            ("unassigned", org, "unassigned@example.test"),
            ("dual-primary", org, "dual@example.test"),
            ("dual-neighbor", neighbor, "dual@example.test"),
            ("neighbor", neighbor, "neighbor@example.test"),
        )
    }
    policies = {
        name: TenantAccessPolicy(organization_id=owner.id, name=name, created_by=owner.created_by)
        for name, owner in (("own", org), ("peer", org), ("dual", org), ("neighbor", neighbor))
    }
    db.add_all([*members.values(), *policies.values()])
    db.sync.flush()
    for name, policy in policies.items():
        db.add(TenantPolicyRule(
            policy_id=policy.id, cluster_id=f"qa-{name}", project_name=f"qa-{name}",
            instance_name="qa-workload", permissions=["view", "operate"],
        ))
    assignments = {
        name: TenantPolicyAssignment(
            policy_id=policies[policy].id, org_member_id=members[member].id,
            assigned_by="partner@example.test",
        )
        for name, policy, member in (
            ("own", "own", "own"),
            ("peer", "peer", "peer"),
            ("neighbor", "neighbor", "neighbor"),
            ("cross-org", "dual", "dual-neighbor"),
        )
    }
    db.add_all(assignments.values())
    db.sync.commit()
    try:
        yield PolicyWorld(db, policies, members, assignments)
    finally:
        db.sync.close()
        db.sync.bind.dispose()


def get_details(world: PolicyWorld, name: str, user: AuthenticatedUser):
    return asyncio.run(tenant_access.get_policy(world.policies[name].id, user, world.db))


def test_assigned_tenant_reads_own_policy_and_rules(world: PolicyWorld):
    response = get_details(world, "own", actor("own@example.test"))
    assert response.id == world.policies["own"].id
    assert response.organization_id == world.members["own"].organization_id
    assert [(rule.cluster_id, rule.project_name, rule.permissions) for rule in response.rules] == [
        ("qa-own", "qa-own", ["view", "operate"]),
    ]


@pytest.mark.parametrize("email,policy", [
    ("unassigned@example.test", "own"),
    ("own@example.test", "peer"),
])
def test_same_org_tenant_cannot_read_an_unassigned_policy(world: PolicyWorld, email: str, policy: str):
    with pytest.raises(HTTPException) as denied:
        get_details(world, policy, actor(email))
    assert (denied.value.status_code, denied.value.detail) == (404, "Policy not found")


def test_assignment_to_same_email_in_another_org_does_not_grant_details(world: PolicyWorld):
    with pytest.raises(HTTPException) as denied:
        get_details(world, "dual", actor("dual@example.test"))
    assert (denied.value.status_code, denied.value.detail) == (404, "Policy not found")


def test_matching_assignment_is_visible_without_replacing_a_loaded_policy(world: PolicyWorld):
    user = actor("unassigned@example.test")
    policy = world.policies["own"]
    assert len(policy.assignments) == 1
    with pytest.raises(HTTPException) as denied:
        get_details(world, "own", user)
    assert denied.value.status_code == 404
    world.db.add(TenantPolicyAssignment(
        policy_id=policy.id, org_member_id=world.members["unassigned"].id,
        assigned_by="partner@example.test",
    ))
    world.db.sync.commit()
    assert get_details(world, "own", user).id == policy.id


@pytest.mark.parametrize("email,roles", [
    ("neighbor@example.test", ("tenant",)),
    ("neighbor-partner@example.test", ("partner",)),
])
def test_neighbor_organization_access_is_still_denied(world: PolicyWorld, email: str, roles: tuple[str, ...]):
    with pytest.raises(HTTPException) as denied:
        get_details(world, "own", actor(email, *roles))
    assert (denied.value.status_code, denied.value.detail) == (403, "Access denied to this organization")


@pytest.mark.parametrize("roles", [("admin",), ("partner",), ("tenant", "partner")])
def test_privileged_policy_management_does_not_require_assignment(world: PolicyWorld, roles: tuple[str, ...]):
    assert get_details(world, "own", actor("partner@example.test", *roles)).id == world.policies["own"].id


def test_admin_can_read_neighbor_policy_without_organization_membership(world: PolicyWorld):
    assert get_details(world, "neighbor", actor("admin@example.test", "admin")).id == world.policies["neighbor"].id


def test_missing_policy_uses_the_same_not_found_response(world: PolicyWorld):
    with pytest.raises(HTTPException) as denied:
        asyncio.run(tenant_access.get_policy(999999, actor("own@example.test"), world.db))
    assert (denied.value.status_code, denied.value.detail) == (404, "Policy not found")


def test_revocation_removes_details_despite_loaded_assignments(world: PolicyWorld, monkeypatch: pytest.MonkeyPatch):
    user = actor("own@example.test")
    assert get_details(world, "own", user).id == world.policies["own"].id
    assert len(world.policies["own"].assignments) == 1
    invalidations: list[bool] = []
    monkeypatch.setattr(tenant_access, "_invalidate_acl_caches", lambda: invalidations.append(True))
    asyncio.run(tenant_access.delete_assignment(
        world.assignments["own"].id, actor("partner@example.test", "partner"), world.db,
    ))
    assert invalidations == [True]
    assert world.db.sync.scalar(select(TenantPolicyAssignment.id).where(
        TenantPolicyAssignment.id == world.assignments["own"].id,
    )) is None
    assert world.db.sync.get(TenantPolicyAssignment, world.assignments["peer"].id) is not None
    with pytest.raises(HTTPException) as denied:
        get_details(world, "own", user)
    assert (denied.value.status_code, denied.value.detail) == (404, "Policy not found")
    assert get_details(world, "peer", actor("peer@example.test")).id == world.policies["peer"].id
