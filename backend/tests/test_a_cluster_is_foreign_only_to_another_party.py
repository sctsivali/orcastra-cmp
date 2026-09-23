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
X = "partner-x@example.test"


@pytest.fixture
def creators(monkeypatch) -> Dict[str, str]:
    known: Dict[str, str] = {"oak_a": A, "oak_b": B, "oak_admin": "admin@example.test"}

    def read(key_ids: Set[str]) -> Dict[str, str]:
        gone = {"oak_deleted": cluster_parties.DELETED_KEY}
        return {k: gone.get(k, known.get(k, "")) for k in key_ids}

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
        # a buyer of A whose key was rotated away, and one whose key an admin minted
        Organization(id=6, name="Buyer of A, old key", slug="buyer-a-old", created_by=A, is_active=True,
                     source="orcahub", api_key_id="oak_deleted"),
        Organization(id=7, name="Buyer of A, admin key", slug="buyer-a-admin", created_by=A, is_active=True,
                     source="orcahub", api_key_id="oak_admin"),
        OrganizationCluster(organization_id=1, cluster_id="rotated"),
        OrganizationCluster(organization_id=6, cluster_id="rotated"),
        OrganizationCluster(organization_id=1, cluster_id="admin-keyed"),
        OrganizationCluster(organization_id=7, cluster_id="admin-keyed"),
        # X runs org 2 with B and also buys from A: a tenant of a buyer org A's key provisioned,
        # which provisioning stamps with A as its creator
        Organization(id=8, name="X buys from A", slug="x-buys", created_by=A, is_active=True,
                     source="orcahub", api_key_id="oak_a"),
        OrganizationCluster(organization_id=1, cluster_id="sold2"),
        OrganizationCluster(organization_id=8, cluster_id="sold2"),
        OrganizationMember(organization_id=2, user_email=X, role="partner"),
        OrganizationMember(organization_id=8, user_email=X, role="tenant"),
        # two partners onboarded through OrcaHub: sync stamps both home orgs "system"
        Organization(id=10, name="A home", slug="a-home", created_by="system", is_active=True,
                     source="orcahub", api_key_id="oak_a"),
        Organization(id=11, name="B home", slug="b-home", created_by="system", is_active=True,
                     source="orcahub", api_key_id="oak_b"),
        OrganizationMember(organization_id=10, user_email=A, role="partner"),
        OrganizationMember(organization_id=11, user_email=B, role="partner"),
        OrganizationCluster(organization_id=10, cluster_id="sys-shared"),
        OrganizationCluster(organization_id=11, cluster_id="sys-shared"),
        # two partners whose orgs one admin created
        Organization(id=12, name="A by admin", slug="a-admin", created_by="admin@example.test", is_active=True),
        Organization(id=13, name="B by admin", slug="b-admin", created_by="admin@example.test", is_active=True),
        OrganizationMember(organization_id=12, user_email=A, role="partner"),
        OrganizationMember(organization_id=13, user_email=B, role="partner"),
        OrganizationCluster(organization_id=12, cluster_id="admin-shared"),
        OrganizationCluster(organization_id=13, cluster_id="admin-shared"),
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


async def test_a_buyer_org_whose_key_was_deleted_holds_nothing(creators) -> None:
    db = await _world()
    assert await cluster_parties.clusters_held_by_another_party(db, A2, {"rotated"}) == set()


async def test_a_buyer_org_on_an_admin_minted_key_still_counts_as_another_party(creators) -> None:
    """Known limit, narrowing: the key's creator is the admin, who is on nobody's team."""
    db = await _world()
    assert await cluster_parties.clusters_held_by_another_party(db, A2, {"admin-keyed"}) == {"admin-keyed"}


async def test_partners_whose_orgs_sync_stamped_system_stay_apart(creators) -> None:
    db = await _world()
    assert "system" not in await cluster_parties.side_of(db, A)
    assert await cluster_parties.clusters_held_by_another_party(db, A, {"sys-shared"}) == {"sys-shared"}
    assert await cluster_parties.clusters_held_by_another_party(db, B, {"sys-shared"}) == {"sys-shared"}


async def test_partners_whose_orgs_one_admin_created_stay_apart(creators) -> None:
    db = await _world()
    assert await cluster_parties.clusters_held_by_another_party(db, A, {"admin-shared"}) == {"admin-shared"}
    assert await cluster_parties.clusters_held_by_another_party(db, B, {"admin-shared"}) == {"admin-shared"}


async def test_buying_from_a_partner_does_not_put_the_buyer_on_the_sellers_side(creators) -> None:
    db = await _world()
    assert A not in await cluster_parties.side_of(db, X)
    assert X not in await cluster_parties.side_of(db, A)
    assert await cluster_parties.clusters_held_by_another_party(db, X, {"sold2"}) == {"sold2"}



def _vault_error(status: int) -> Exception:
    """The shape vault_client.vault_read raises: VaultClientError from a requests HTTPError."""
    import requests

    from app.core.vault_client import VaultClientError

    response = requests.Response()
    response.status_code = status
    try:
        try:
            raise requests.exceptions.HTTPError(f"{status}", response=response)
        except requests.exceptions.HTTPError as http_error:
            raise VaultClientError(f"Vault read failed: {http_error}")
    except VaultClientError as exc:
        return exc


def test_a_404_from_vault_reads_as_a_deleted_key_and_nothing_else_does() -> None:
    assert cluster_parties._is_not_found(_vault_error(404))
    assert not cluster_parties._is_not_found(_vault_error(403))
    assert not cluster_parties._is_not_found(_vault_error(500))
    assert not cluster_parties._is_not_found(RuntimeError("connection reset"))


def test_the_creator_reader_marks_only_a_404_as_deleted(monkeypatch) -> None:
    from types import SimpleNamespace

    from app.core import config, vault_client

    class _Vault:
        def vault_read(self, path: str) -> Dict[str, str]:
            if "/oak_gone/" in path:
                raise _vault_error(404)
            if "/oak_flaky/" in path:
                raise _vault_error(500)
            return {"created_by": A}

    monkeypatch.setattr(config, "get_settings",
                        lambda: SimpleNamespace(vault_enabled=True, vault_token="t", vault_addr="http://vault"))
    monkeypatch.setattr(vault_client, "get_vault_client", lambda *_a: _Vault())
    creators = cluster_parties.read_key_creators_blocking({"oak_gone", "oak_flaky", "oak_ok"})
    assert creators == {"oak_gone": cluster_parties.DELETED_KEY, "oak_flaky": "", "oak_ok": A}
