"""Whose branding an account sees when it belongs to several organizations (services/branding/subject.py).

The old rule took the earliest membership, compared case-sensitively, so a partner who had once been
added to a customer's organization saw that customer's look, and an address Authentik spelled with
capitals matched nothing at all. An Administrator without the partner role sees the deployment
default rather than whichever organization it joined first.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models.branding_models import DeploymentBranding
from app.models.db_models import (
    Organization,
    OrganizationMember,
    OrganizationPreferences,
    TenantAccessPolicy,
    TenantPolicyAssignment,
)
from app.services.branding import store
from app.services.branding.subject import subject_organization_id
from tests._sqlite_session import org_tables, sqlite_session, with_json_tables

ME = "Person@Example.test"
NOW = datetime.now(timezone.utc)
EARLY = NOW - timedelta(days=30)


async def _world(*memberships):
    """memberships: (org_id, role, joined_at, created_by, source, active, assignment_expires)."""
    db = with_json_tables(
        sqlite_session(*org_tables()), OrganizationPreferences, DeploymentBranding, TenantAccessPolicy,
    )
    for org_id, _role, _joined, created_by, source, active, _exp in memberships:
        db.add(Organization(id=org_id, name=f"Org {org_id}", slug=f"org-{org_id}", created_by=created_by,
                            source=source, is_active=active))
    await db.commit()
    for org_id, role, joined, _created_by, _source, _active, expires in memberships:
        member = OrganizationMember(organization_id=org_id, user_email="person@example.test", role=role,
                                    joined_at=joined)
        db.add(member)
        await db.commit()
        if expires is not None:
            db.add(TenantAccessPolicy(id=org_id * 10, organization_id=org_id, name=f"auto:{org_id}",
                                      source="orcahub", created_by="orcahub-provisioning"))
            await db.commit()
            db.add(TenantPolicyAssignment(policy_id=org_id * 10, org_member_id=member.id, assigned_by="x",
                                          expires_at=None if expires == "never" else expires))
            await db.commit()
    return db


async def test_an_organization_it_runs_wins_over_an_earlier_membership() -> None:
    db = await _world(
        (1, "tenant", EARLY, "system", "orcahub", True, None),
        (2, "partner", NOW, "system", "orcahub", True, None),
    )
    assert await subject_organization_id(db, ME) == 2


async def test_one_it_created_here_counts_as_one_it_runs() -> None:
    db = await _world(
        (1, "member", EARLY, "someone@else.test", "local", True, None),
        (2, "member", NOW, "PERSON@example.test", "local", True, None),
    )
    assert await subject_organization_id(db, ME) == 2


async def test_a_buyer_organization_its_minter_is_stamped_on_does_not_count_as_run() -> None:
    # Provisioning stamps the seller's key minter as created_by on every buyer it makes.
    db = await _world(
        (1, "member", EARLY, "person@example.test", "orcahub", True, None),
        (2, "member", NOW, "person@example.test", "local", True, None),
    )
    assert await subject_organization_id(db, ME) == 2


async def test_a_tenant_sees_the_organization_where_its_access_is_live() -> None:
    db = await _world(
        (1, "tenant", EARLY, "system", "orcahub", True, NOW - timedelta(days=1)),
        (2, "tenant", NOW, "system", "orcahub", True, "never"),
    )
    assert await subject_organization_id(db, ME) == 2


async def test_an_inactive_organization_is_never_the_answer() -> None:
    db = await _world(
        (1, "partner", EARLY, "system", "orcahub", False, None),
        (2, "tenant", NOW, "system", "orcahub", True, None),
    )
    assert await subject_organization_id(db, ME) == 2


async def test_an_account_in_no_organization_sees_the_deployment_default() -> None:
    db = await _world()
    db.add(DeploymentBranding(id=1, settings={"branding": {"appName": "Acme"}}, updated_by="admin@x"))
    await db.commit()

    account = await store.account_branding(db, ME, admin_only=False)

    assert (account.organization_id, account.subject) == (0, "deployment")
    assert account.resolved.settings == {"branding": {"appName": "Acme"}}


async def test_an_administrator_without_the_partner_role_sees_the_deployment_default() -> None:
    db = await _world((1, "partner", EARLY, "system", "orcahub", True, None))
    db.add(OrganizationPreferences(organization_id=1, settings={"theme": "dark"}, updated_by="p@x"))
    await db.commit()

    account = await store.account_branding(db, ME, admin_only=True)

    assert (account.organization_id, account.subject, account.resolved.source) == (0, "deployment", "default")
