"""A cluster counts as held by another party only when a holder is not on the caller's side.

Provisioning binds every buyer organization to the partner's cluster, so counting bindings made
every partner cluster with a sale look shared. The whole-cluster checks and the key rules ask by
party instead: a buyer organization is on the side of whoever minted its key, and a key's minter
is on the side of everyone in their organizations. These run the real queries on SQLite rows.
"""

from typing import Dict, Set

import pytest

from app.models.db_models import Organization, OrganizationCluster, OrganizationMember
from app.services import cluster_parties
from tests._sqlite_session import sqlite_session

A = "partner-a@example.test"
A2 = "partner-a2@example.test"
B = "partner-b@example.test"


@pytest.fixture
def creators(monkeypatch) -> Dict[str, str]:
    known: Dict[str, str] = {"oak_a": A, "oak_b": B}

    def read(key_ids: Set[str]) -> Dict[str, str]:
        return {k: known.get(k, "") for k in key_ids}

    monkeypatch.setattr(cluster_parties, "read_key_creators_blocking", read)
    return known


async def _world():
    db = sqlite_session(Organization, OrganizationMember, OrganizationCluster)
    db.add_all([
        Organization(id=1, name="A", slug="a", created_by=A, is_active=True),
        Organization(id=2, name="B", slug="b", created_by=B, is_active=True),
        Organization(id=3, name="Buyer of A", slug="buyer-a", created_by="system", is_active=True,
                     source="orcahub", api_key_id="oak_a"),
        Organization(id=4, name="Buyer of B", slug="buyer-b", created_by="system", is_active=True,
                     source="orcahub", api_key_id="oak_b"),
        Organization(id=5, name="Unknown key", slug="unknown", created_by="system", is_active=True,
                     source="orcahub", api_key_id="oak_gone"),
        OrganizationMember(organization_id=1, user_email=A, role="partner"),
        OrganizationMember(organization_id=1, user_email=A2, role="partner"),
        OrganizationMember(organization_id=2, user_email=B, role="partner"),
        OrganizationCluster(organization_id=1, cluster_id="sold"),
        OrganizationCluster(organization_id=3, cluster_id="sold"),
        OrganizationCluster(organization_id=1, cluster_id="shared"),
        OrganizationCluster(organization_id=2, cluster_id="shared"),
        OrganizationCluster(organization_id=1, cluster_id="resold"),
        OrganizationCluster(organization_id=4, cluster_id="resold"),
        OrganizationCluster(organization_id=1, cluster_id="mystery"),
        OrganizationCluster(organization_id=5, cluster_id="mystery"),
        OrganizationCluster(organization_id=None, cluster_id="squat", registered_by=B),
        OrganizationCluster(organization_id=None, cluster_id="mine", registered_by=A),
    ])
    await db.commit()
    return db


ALL = {"sold", "shared", "resold", "mystery", "squat", "mine"}


async def test_the_partners_own_buyers_do_not_make_its_cluster_foreign(creators) -> None:
    db = await _world()
    held = await cluster_parties.clusters_held_by_another_party(db, A, ALL)
    assert held == {"shared", "resold", "mystery", "squat"}


async def test_a_teammate_is_on_the_same_side(creators) -> None:
    db = await _world()
    assert "sold" not in await cluster_parties.clusters_held_by_another_party(db, A2, ALL)


async def test_the_other_partner_sees_the_first_ones_cluster_as_foreign(creators) -> None:
    db = await _world()
    held = await cluster_parties.clusters_held_by_another_party(db, B, {"sold", "resold"})
    assert held == {"sold", "resold"}


async def test_the_side_of_a_user_is_their_organizations(creators) -> None:
    db = await _world()
    assert await cluster_parties.side_of(db, A) == {A, A2}
    assert await cluster_parties.side_of(db, "Nobody@Example.test") == {"nobody@example.test"}


async def test_a_list_keeps_every_bounded_cluster_when_the_party_check_stalls(monkeypatch) -> None:
    from fastapi import HTTPException

    async def stall(*_a, **_k):
        raise HTTPException(status_code=503, detail="Could not confirm who holds these clusters. Try again.")

    monkeypatch.setattr(cluster_parties, "clusters_held_by_another_party", stall)
    assert await cluster_parties.held_or_every_bounded(None, A, {"sold", "shared"}) == {"sold", "shared"}


async def test_a_list_still_raises_anything_but_a_stall(monkeypatch) -> None:
    from fastapi import HTTPException

    async def boom(*_a, **_k):
        raise HTTPException(status_code=500, detail="bug")

    monkeypatch.setattr(cluster_parties, "clusters_held_by_another_party", boom)
    with pytest.raises(HTTPException):
        await cluster_parties.held_or_every_bounded(None, A, {"sold"})

