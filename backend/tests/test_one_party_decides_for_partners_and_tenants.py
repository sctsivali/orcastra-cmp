"""The partner bound is decided by party and a tenant's '*' by organization, on real rows.

A tenant of one buyer with a '*' rule reached a sibling buyer's projects on the partner's cluster,
since every buyer carries the partner's key and a buyer synced from OrcaHub carries a partner row.
A tenant's '*' now stands only where its own organization alone holds the cluster. A partner
reached through a member row in another partner's organization went unbounded on that partner's
cluster while it had one binding, whole-cluster writes included, and its bound is now asked of
every cluster it reaches. These run the real get_tenant_acl_rules, project_bound_for and
get_visible_projects over SQLite rows, with no party stub.
"""

from typing import List, Optional, Tuple

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles

from app.core import org_scope
from app.core.auth import AuthenticatedUser
from app.core.instance_access import verify_whole_cluster_access
from app.core.tenant_wildcard import project_bound_for
from app.models.db_models import (
    Organization,
    OrganizationCluster,
    OrganizationMember,
    OrganizationOwnerKey,
    TenantAccessPolicy,
    TenantPolicyAssignment,
    TenantPolicyRule,
)
from tests._sqlite_session import sqlite_session


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(_element, _compiler, **_kw):  # the policy tables carry a JSONB column
    return "JSON"


PARTNER = "p@example.com"
TABLES = (Organization, OrganizationMember, OrganizationCluster, TenantAccessPolicy, TenantPolicyRule,
          TenantPolicyAssignment, OrganizationOwnerKey)


def _user(email: str, role: str) -> AuthenticatedUser:
    return AuthenticatedUser(user_id=email, email=email, groups=[f"role_{role}"])


async def _world(orgs: List[Tuple[int, str, Optional[str], str]], members: List[Tuple[int, str, str]],
                 bindings: List[Tuple[int, str]], rules: List[Tuple[int, str, str]] = (),
                 star: Optional[Tuple[int, str, str]] = None):
    """orgs (id, created_by, key, source), members (org, email, role), bindings (org, cluster),
    rules (org, cluster, project) provisioned as concrete policies, star (org, cluster, email) a '*'
    policy assigned to that member."""
    db = sqlite_session(*TABLES)
    db.add_all([Organization(id=i, name=f"o{i}", slug=f"o{i}", created_by=by, is_active=True, api_key_id=key,
                             source=source) for i, by, key, source in orgs])
    await db.commit()
    rows = [OrganizationMember(organization_id=o, user_email=e, role=r) for o, e, r in members]
    rows += [OrganizationCluster(organization_id=b[0], cluster_id=b[1], registered_by=(b[2] if len(b) > 2 else None))
             for b in bindings]
    db.add_all(rows)
    await db.commit()
    for n, (org, cluster, project) in enumerate(rules, start=1):
        policy = TenantAccessPolicy(id=100 + n, organization_id=org, name=f"auto-{n}", created_by="system")
        db.add(policy)
        await db.commit()
        db.add(TenantPolicyRule(policy_id=policy.id, cluster_id=cluster, project_name=project, instance_name="*",
                                permissions=["view", "operate"]))
        await db.commit()
    if star:
        org, cluster, email = star
        policy = TenantAccessPolicy(id=900, organization_id=org, name="everything", created_by=PARTNER)
        db.add(policy)
        await db.commit()
        db.add(TenantPolicyRule(policy_id=900, cluster_id=cluster, project_name="*", instance_name="*",
                                permissions=["view", "operate"]))
        member = next(m for m in rows if isinstance(m, OrganizationMember) and m.user_email == email
                      and m.organization_id == org)
        db.add(TenantPolicyAssignment(policy_id=900, org_member_id=member.id, assigned_by=PARTNER))
        await db.commit()
    return db


async def _tenant_view(db, email: str, cluster: str) -> Tuple[set, Optional[set]]:
    """The projects the tenant's ACL names on `cluster`, and its project bound there (None: every project)."""
    user = _user(email, "tenant")
    acl = await org_scope.get_tenant_acl_rules(user, db)
    bound = await project_bound_for(user, db, acl)
    return {project for project, _ in (acl or {}).get(cluster, {})}, (bound or {}).get(cluster)


# a partner's home, synced with its partner row, and two buyers its key provisioned
SALE = dict(
    orgs=[(1, "system", "oak_a", "orcahub"), (2, PARTNER, "oak_a", "orcahub"), (3, PARTNER, "oak_a", "orcahub")],
    members=[(1, PARTNER, "partner"), (1, "home@example.com", "tenant"), (2, "ta@example.com", "tenant"),
             (3, "tb@example.com", "tenant")],
    bindings=[(1, "own", PARTNER), (2, "own"), (3, "own")],
    rules=[(2, "own", "a-shop"), (3, "own", "b-shop")],
)


async def test_a_buyers_wildcard_stays_inside_the_buyer() -> None:
    db = await _world(**SALE, star=(2, "own", "ta@example.com"))
    named, bound = await _tenant_view(db, "ta@example.com", "own")
    assert "*" not in named and "b-shop" not in named, "a buyer's '*' reached its sibling buyer"
    assert bound == {"default", "a-shop"}


async def test_a_synced_buyer_with_a_founder_partner_row_stays_inside_the_buyer() -> None:
    """OrcaHub makes every organization's founder a PARTNER, and the sync keeps that role."""
    world = dict(SALE, members=SALE["members"] + [(2, "founder@example.com", "partner")])
    db = await _world(**world, star=(2, "own", "ta@example.com"))
    named, bound = await _tenant_view(db, "ta@example.com", "own")
    assert bound == {"default", "a-shop"}


async def test_a_home_wildcard_covers_its_named_projects_once_a_buyer_holds_the_cluster() -> None:
    """The narrowing this rule accepts: the home's '*' no longer names its buyers' projects, nor
    the partner's projects its policies do not name."""
    world = dict(SALE, rules=SALE["rules"] + [(1, "own", "home-web")])
    db = await _world(**world, star=(1, "own", "home@example.com"))
    named, bound = await _tenant_view(db, "home@example.com", "own")
    assert bound == {"default", "home-web"} and "*" not in named


async def test_a_wildcard_on_a_cluster_its_org_alone_holds_means_every_project() -> None:
    world = dict(SALE, bindings=[(1, "own"), (1, "solo"), (2, "own"), (3, "own")])
    db = await _world(**world, star=(1, "solo", "home@example.com"))
    named, bound = await _tenant_view(db, "home@example.com", "solo")
    assert "*" in named and bound is None


async def test_a_rule_on_a_cluster_its_org_no_longer_holds_grants_nothing() -> None:
    """Unbinding leaves the rules behind. The buyer left 'own', the home holds it alone, and a
    tenant of both would otherwise ride the buyer's old '*' into every project there, unbounded."""
    db = await _world(
        orgs=[(1, "system", "oak_a", "orcahub"), (2, PARTNER, "oak_a", "orcahub")],
        members=[(1, PARTNER, "partner"), (1, "t@example.com", "tenant"), (2, "t@example.com", "tenant")],
        bindings=[(1, "own")],
        star=(2, "own", "t@example.com"),
    )
    named, bound = await _tenant_view(db, "t@example.com", "own")
    assert named == set() and bound is None
    user = _user("t@example.com", "tenant")
    acl = await org_scope.get_tenant_acl_rules(user, db)
    assert org_scope._narrow_by_tenant_acl(await org_scope.get_user_allowed_clusters(user, db), acl) == set()


async def test_an_unbound_row_confines_a_wildcard_like_another_org() -> None:
    world = dict(SALE, bindings=[(1, "solo"), (None, "solo")])
    db = await _world(**world, star=(1, "solo", "home@example.com"))
    named, bound = await _tenant_view(db, "home@example.com", "solo")
    assert "*" not in named and bound == {"default"}


# -- the partner bound, over the clusters the partner reaches ---------------------------------


async def _partner_scope(db, email: str) -> Tuple[Optional[set], Optional[dict]]:
    user = _user(email, "partner")
    allowed = await org_scope.get_user_allowed_clusters(user, db)
    return allowed, await org_scope.get_visible_projects(user, db, allowed)


async def test_a_partner_is_bounded_on_another_partners_single_binding_cluster() -> None:
    """p1 holds a tenant row in p2's home, the only binding on c2: p1 reaches c2 and is bounded there."""
    db = await _world(
        orgs=[(1, "system", "oak_1", "orcahub"), (2, "system", "oak_2", "orcahub")],
        members=[(1, PARTNER, "partner"), (2, "p2@example.com", "partner"), (2, PARTNER, "tenant")],
        bindings=[(1, "c1"), (2, "c2")],
    )
    allowed, bound = await _partner_scope(db, PARTNER)
    assert allowed == {"c1", "c2"}
    assert bound == {"c2": {"default"}}, "a single binding of another party left the partner unbounded"
    token = org_scope._allowed_clusters_var.set(allowed)
    visible = org_scope._visible_projects_var.set(bound)
    try:
        with pytest.raises(HTTPException) as caught:
            await verify_whole_cluster_access("c2", db, PARTNER)
        assert caught.value.status_code == 403
        await verify_whole_cluster_access("c1", db, PARTNER)
    finally:
        org_scope._visible_projects_var.reset(visible)
        org_scope._allowed_clusters_var.reset(token)
    other_allowed, other_bound = await _partner_scope(db, "p2@example.com")
    assert other_allowed == {"c2"} and other_bound is None, "p2's own single-binding cluster got a bound"


async def test_a_partner_is_bounded_only_on_clusters_it_reaches() -> None:
    db = await _world(
        orgs=[(1, "system", "oak_1", "orcahub"), (2, "system", "oak_2", "orcahub"), (3, "system", "oak_3", "orcahub")],
        members=[(1, PARTNER, "partner"), (2, "p2@example.com", "partner"), (3, "p3@example.com", "partner")],
        bindings=[(1, "c1"), (1, "mixed"), (2, "mixed"), (2, "far"), (3, "far")],
    )
    allowed, bound = await _partner_scope(db, PARTNER)
    assert allowed == {"c1", "mixed"} and bound == {"mixed": {"default"}}


async def test_the_partner_keeps_every_project_on_its_own_cluster_after_sales() -> None:
    db = await _world(**SALE)
    allowed, bound = await _partner_scope(db, PARTNER)
    assert allowed == {"own"} and bound is None


# -- a synced buyer founder is not on the seller's side --------------------------------------

FOUNDER = "founder@example.com"


async def test_a_synced_buyer_founder_is_bounded_on_the_sellers_cluster() -> None:
    """OrcaHub makes every founder a PARTNER, so the sync writes a partner row in the buyer. That row
    must not lend the buyer's key (the seller's) to the founder's side."""
    world = dict(SALE, members=SALE["members"] + [(2, FOUNDER, "partner")])
    db = await _world(**world)
    allowed, bound = await _partner_scope(db, FOUNDER)
    assert allowed == {"own"} and bound == {"own": {"default", "a-shop"}}
    token = org_scope._allowed_clusters_var.set(allowed)
    visible = org_scope._visible_projects_var.set(bound)
    try:
        with pytest.raises(HTTPException) as caught:
            await verify_whole_cluster_access("own", db, FOUNDER)
        assert caught.value.status_code == 403
    finally:
        org_scope._visible_projects_var.reset(visible)
        org_scope._allowed_clusters_var.reset(token)
    from app.routers.integrations import _keys_the_caller_manages

    seller_key = {"id": "oak_a", "created_by": PARTNER,
                  "cluster_permissions": [{"cluster_id": "own", "permissions": ["read"], "projects": ["*"]}]}
    assert await _keys_the_caller_manages(db, _user(FOUNDER, "partner"), [seller_key]) == [], (
        "a buyer founder could revoke or delete the seller's integration key")
    assert await _keys_the_caller_manages(db, _user(PARTNER, "partner"), [seller_key]) == [seller_key]


async def test_the_seller_keeps_its_cluster_when_an_admin_minted_the_buyers() -> None:
    """The buyers carry an admin as created_by, so only the home's key joins them to the seller."""
    world = dict(SALE, orgs=[(1, "system", "oak_a", "orcahub"), (2, "ops@example.com", "oak_a", "orcahub"),
                             (3, "ops@example.com", "oak_a", "orcahub")],
                 members=SALE["members"] + [(2, FOUNDER, "partner")])
    db = await _world(**world)
    assert (await _partner_scope(db, PARTNER))[1] is None
    assert (await _partner_scope(db, FOUNDER))[1] == {"own": {"default", "a-shop"}}


async def test_a_home_synced_again_after_a_buyer_lends_no_key_to_anyone() -> None:
    """The seller's home was deleted and the sync created it again, so a buyer the sync created
    earlier is now the oldest org on the key. That buyer registered no cluster, so no partner row
    lends the key: the founder stays bounded, and the seller is narrowed until the home is fixed."""
    db = await _world(
        orgs=[(2, "system", "oak_a", "orcahub"), (3, "ops@example.com", "oak_a", "orcahub"),
              (5, "system", "oak_a", "orcahub")],
        members=[(2, FOUNDER, "partner"), (5, PARTNER, "partner")],
        bindings=[(2, "own"), (3, "own"), (5, "own", PARTNER)],
        rules=[(2, "own", "a-shop"), (3, "own", "b-shop"), (5, "own", "home-web")],
    )
    from app.services.cluster_parties import side_keys

    assert await side_keys(db, FOUNDER) == set() and await side_keys(db, PARTNER) == set()
    assert (await _partner_scope(db, FOUNDER))[1] == {"own": {"default", "a-shop"}}
    assert (await _partner_scope(db, PARTNER))[1] == {"own": {"default", "home-web"}}


async def test_a_founder_registering_a_cluster_of_its_own_still_lends_no_key() -> None:
    """The buyer is the oldest synced org on the key and its founder registers a private cluster:
    that cluster carries no other org on the key, so it proves nothing about the key's home."""
    db = await _world(
        orgs=[(2, "system", "oak_a", "orcahub"), (3, "ops@example.com", "oak_a", "orcahub"),
              (5, "system", "oak_a", "orcahub")],
        members=[(2, FOUNDER, "partner"), (5, PARTNER, "partner")],
        bindings=[(2, "own"), (3, "own"), (5, "own", PARTNER), (2, "founders-own", FOUNDER)],
        rules=[(2, "own", "a-shop"), (3, "own", "b-shop"), (5, "own", "home-web")],
    )
    from app.services.cluster_parties import side_keys

    assert await side_keys(db, FOUNDER) == set()
    assert (await _partner_scope(db, FOUNDER))[1]["own"] == {"default", "a-shop"}


async def test_a_home_whose_cluster_a_member_registered_lends_no_key() -> None:
    """Only a partner row counts as running the home, for the registrant as for everything else."""
    world = dict(SALE, members=SALE["members"] + [(1, "mate@example.com", "member")],
                 bindings=[(1, "own", "mate@example.com"), (2, "own"), (3, "own")],
                 orgs=[(1, "system", "oak_a", "orcahub"), (2, "ops@example.com", "oak_a", "orcahub"),
                       (3, "ops@example.com", "oak_a", "orcahub")])
    db = await _world(**world)
    from app.services.cluster_parties import side_keys

    assert await side_keys(db, PARTNER) == set()


async def test_a_founder_binding_the_sellers_cluster_again_lends_no_key() -> None:
    """The founder unbinds and binds the seller's cluster again, so the buyer's binding carries the
    founder as registrant on a cluster the key sold on. It is newer than the other buyers' bindings."""
    db = await _world(
        orgs=[(2, "system", "oak_a", "orcahub"), (3, "ops@example.com", "oak_a", "orcahub"),
              (5, "system", "oak_a", "orcahub")],
        members=[(2, FOUNDER, "partner"), (5, PARTNER, "partner")],
        bindings=[(5, "own", PARTNER), (3, "own"), (2, "own", FOUNDER)],
        rules=[(2, "own", "a-shop"), (3, "own", "b-shop"), (5, "own", "home-web")],
    )
    from app.services.cluster_parties import side_keys

    assert await side_keys(db, FOUNDER) == set()
