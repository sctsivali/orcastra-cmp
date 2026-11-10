"""A partner is held to a project bound on a cluster another party holds, even with one binding.

A partner who bought from another partner reaches that partner's cluster through a member row. Where
only that one organization is bound, counting bindings (org_scope._clusters_with_several_orgs) does
not mark the cluster shared, so the partner would see every project on it and the write policy would
call it EXCLUSIVE, which allows host-level config. The party is asked too. The rows are real on
SQLite.
"""

import pytest
from sqlalchemy import select

from app.core import org_scope
from app.core.auth import AuthenticatedUser
from app.models.db_models import OrganizationMember, TenantAccessPolicy, TenantPolicyAssignment, TenantPolicyRule
from app.services.instance_security_policy import ClusterOwnership, resolve_cluster_write_policy
from tests.test_whole_cluster_state_needs_the_whole_cluster import MIXED, OTHER, OWN, SELLER, SOLO, _world


def _partner(email: str) -> AuthenticatedUser:
    return AuthenticatedUser(user_id=f"role_partner:{email}", email=email, groups=["role_partner"])


async def test_the_cluster_is_bounded_once_the_request_scope_reaches_it() -> None:
    db = await _world(TenantAccessPolicy, TenantPolicyRule, TenantPolicyAssignment)
    bound = await org_scope.get_visible_projects(_partner(SELLER), db, reachable={OWN, MIXED, SOLO})
    assert bound is not None and bound.get(SOLO) == {"default"}
    # Tightening only: 'own' carries a sale, so it is counted shared and stays bounded.
    assert set(bound) == {OWN, MIXED, SOLO}


async def test_the_holder_itself_is_not_bounded_on_its_own_cluster() -> None:
    db = await _world(TenantAccessPolicy, TenantPolicyRule, TenantPolicyAssignment)
    bound = await org_scope.get_visible_projects(_partner(OTHER), db, reachable={SOLO, MIXED})
    assert SOLO not in (bound or {})


@pytest.mark.parametrize("email,ownership", [
    (SELLER, ClusterOwnership.SHARED), (OTHER, ClusterOwnership.EXCLUSIVE),
], ids=["a-buyer-partner-is-held-to-the-safe-allowlist", "the-holder-keeps-full-access"])
async def test_the_write_policy_asks_the_party_too(email: str, ownership: ClusterOwnership) -> None:
    db = await _world()
    assert (await resolve_cluster_write_policy(_partner(email), SOLO, db)).ownership == ownership


async def test_a_project_a_partner_creates_is_recorded_only_under_an_organization_it_runs() -> None:
    """A member row reaches 'solo', but its project's rule must not go into the seller's own policy."""
    db = await _world(TenantAccessPolicy, TenantPolicyRule, TenantPolicyAssignment)
    assert await org_scope.org_to_hold_project_grant(SELLER, SOLO, db) is None
    assert await org_scope.org_to_hold_project_grant(OTHER, SOLO, db) == 4
    assert await org_scope.org_to_hold_project_grant(SELLER, OWN, db) in (1, 2)


async def test_a_teammate_sees_the_projects_assigned_to_its_own_member_row() -> None:
    """The bound widens by assignment too: a teammate who runs no organization is given projects."""
    db = await _world(TenantAccessPolicy, TenantPolicyRule, TenantPolicyAssignment)
    member = (await db.execute(
        select(OrganizationMember.id).where(OrganizationMember.organization_id == 4, OrganizationMember.user_email == SELLER)
    )).scalar_one()
    policy = TenantAccessPolicy(organization_id=4, name="team", created_by=OTHER)
    other = TenantAccessPolicy(organization_id=4, name="not-mine", created_by=OTHER)
    db.add_all([policy, other])
    await db.commit()
    db.add_all([
        TenantPolicyRule(policy_id=policy.id, cluster_id=SOLO, project_name="team-web", permissions=["view"]),
        TenantPolicyRule(policy_id=other.id, cluster_id=SOLO, project_name="someone-else", permissions=["view"]),
        TenantPolicyAssignment(policy_id=policy.id, org_member_id=member, assigned_by=OTHER),
    ])
    await db.commit()
    bound = await org_scope.get_visible_projects(_partner(SELLER), db, reachable={SOLO})
    assert bound[SOLO] == {"default", "team-web"}
