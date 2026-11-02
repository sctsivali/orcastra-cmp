"""A partner is held to a project bound on a cluster another party holds, even with one binding.

The partner bound counted bindings (org_scope._clusters_with_several_orgs). A partner who bought from
another partner reaches that partner's cluster through a member row, and where only that one
organization is bound nothing counted it shared: the partner saw every project on it and the write
policy called it EXCLUSIVE, which allows host-level config. The party is now asked too. The rows are
real on SQLite.
"""

import pytest

from app.core import org_scope
from app.core.auth import AuthenticatedUser
from app.models.db_models import TenantAccessPolicy, TenantPolicyRule
from app.services.instance_security_policy import ClusterOwnership, resolve_cluster_write_policy
from tests.test_whole_cluster_state_needs_the_whole_cluster import MIXED, OTHER, OWN, SELLER, SOLO, _world


def _partner(email: str) -> AuthenticatedUser:
    return AuthenticatedUser(user_id=f"role_partner:{email}", email=email, groups=["role_partner"])


async def test_the_cluster_is_bounded_once_the_request_scope_reaches_it() -> None:
    db = await _world(TenantAccessPolicy, TenantPolicyRule)
    bound = await org_scope.get_visible_projects(_partner(SELLER), db, reachable={OWN, MIXED, SOLO})
    assert bound is not None and bound.get(SOLO) == {"default"}
    # note: tightening only. 'own' carries a sale, so it is counted shared and stays bounded.
    assert set(bound) == {OWN, MIXED, SOLO}


async def test_the_holder_itself_is_not_bounded_on_its_own_cluster() -> None:
    db = await _world(TenantAccessPolicy, TenantPolicyRule)
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
    db = await _world(TenantAccessPolicy, TenantPolicyRule)
    assert await org_scope.org_to_hold_project_grant(SELLER, SOLO, db) is None
    assert await org_scope.org_to_hold_project_grant(OTHER, SOLO, db) == 4
    assert await org_scope.org_to_hold_project_grant(SELLER, OWN, db) in (1, 2)
