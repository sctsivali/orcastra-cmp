"""A partner who reaches a cluster only through a purchase never gets the exclusive write policy.

OrcaHub makes every buyer founder a partner row in the buyer organization, and provisioning binds
that organization to the seller's cluster with no registrant. While the seller's own binding is
there, the cluster has two bound organizations and the founder's writes are held to the shared
policy. If the seller unbinds its own organization, the buyer's binding is the only one left, the
founder runs that organization, and the binding-count rule read EXCLUSIVE: privileged containers,
raw.lxc and host disks on the seller's host. A purchase-only reach (core/sale_term.py) is SHARED.
"""

from typing import Any

from app.core.auth import AuthenticatedUser
from app.models.db_models import Organization, OrganizationCluster, OrganizationMember
from app.services.instance_security_policy import ClusterOwnership, resolve_cluster_write_policy
from tests._sqlite_session import org_tables, sqlite_session

FOUNDER = "founder@buyer.example.test"
SELLER = "seller@partner.example.test"


def _partner(email: str) -> AuthenticatedUser:
    return AuthenticatedUser(user_id=email, email=email, groups=["role_partner"])


async def _world(*, seller_bound: bool) -> Any:
    db = sqlite_session(*org_tables())
    rows: list = [
        Organization(id=2, name="Buyer", slug="buyer", created_by=SELLER, is_active=True, source="orcahub"),
        OrganizationMember(organization_id=2, user_email=FOUNDER, role="partner"),
        OrganizationCluster(organization_id=2, cluster_id="c1", registered_by=None, granted_by=None),
    ]
    if seller_bound:
        rows += [
            Organization(id=1, name="Seller", slug="seller", created_by=SELLER, is_active=True, source="local"),
            OrganizationCluster(organization_id=1, cluster_id="c1", registered_by=SELLER),
        ]
    db.add_all(rows)
    await db.commit()
    return db


async def test_a_founder_whose_buyer_binding_is_the_only_one_is_held_to_the_shared_policy() -> None:
    db = await _world(seller_bound=False)
    policy = await resolve_cluster_write_policy(_partner(FOUNDER), "c1", db)
    assert policy.ownership == ClusterOwnership.SHARED
    assert policy.requires_safe_allowlist is True


async def test_a_founder_beside_the_seller_stays_shared() -> None:
    db = await _world(seller_bound=True)
    policy = await resolve_cluster_write_policy(_partner(FOUNDER), "c1", db)
    assert policy.ownership == ClusterOwnership.SHARED


async def test_a_seller_alone_on_its_registered_cluster_stays_exclusive() -> None:
    db = sqlite_session(*org_tables())
    db.add_all([
        Organization(id=1, name="Seller", slug="seller", created_by=SELLER, is_active=True, source="local"),
        OrganizationCluster(organization_id=1, cluster_id="c1", registered_by=SELLER),
    ])
    await db.commit()
    policy = await resolve_cluster_write_policy(_partner(SELLER), "c1", db)
    assert policy.ownership == ClusterOwnership.EXCLUSIVE


async def test_an_admin_grant_is_not_a_purchase() -> None:
    db = sqlite_session(*org_tables())
    db.add_all([
        Organization(id=2, name="Synced", slug="synced", created_by="system", is_active=True, source="orcahub"),
        OrganizationMember(organization_id=2, user_email=FOUNDER, role="partner"),
        OrganizationCluster(organization_id=2, cluster_id="c1", registered_by=None, granted_by="ops@example.test"),
    ])
    await db.commit()
    policy = await resolve_cluster_write_policy(_partner(FOUNDER), "c1", db)
    assert policy.ownership == ClusterOwnership.EXCLUSIVE, "an administrator lent it outright"


# The same purchase rule for the whole-cluster objects: storage pools, LXD auth, the cluster's trust.

async def test_a_founder_whose_purchase_is_the_only_binding_cannot_touch_whole_cluster_objects() -> None:
    import pytest
    from fastapi import HTTPException

    from app.core import whole_cluster_access

    db = await _world(seller_bound=False)
    founder = _partner(FOUNDER)
    assert await whole_cluster_access.the_party_holds_it_whole("c1", db, founder) is False
    assert await whole_cluster_access.clusters_held_elsewhere(["c1"], db, founder) == {"c1"}
    with pytest.raises(HTTPException) as refused:
        await whole_cluster_access.refuse_unless_the_party_holds_it_whole("c1", db, founder)
    assert refused.value.status_code == 403


async def test_a_seller_alone_on_its_cluster_keeps_its_whole_cluster_objects() -> None:
    from app.core import whole_cluster_access

    db = sqlite_session(*org_tables())
    db.add_all([
        Organization(id=1, name="Seller", slug="seller", created_by=SELLER, is_active=True, source="local"),
        OrganizationCluster(organization_id=1, cluster_id="c1", registered_by=SELLER),
    ])
    await db.commit()
    seller = _partner(SELLER)
    assert await whole_cluster_access.the_party_holds_it_whole("c1", db, seller) is True
    assert await whole_cluster_access.clusters_held_elsewhere(["c1"], db, seller) == set()
