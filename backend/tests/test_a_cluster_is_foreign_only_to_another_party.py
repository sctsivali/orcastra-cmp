"""A cluster counts as held by another party only when a holder is outside the caller's party.

Provisioning binds every buyer organization to the partner's cluster, so counting bindings made
every partner cluster with a sale look shared. The party is decided from the database alone: the
organizations the caller runs (created, or joined above tenant), plus any organization keyed to
the same integration key as one of those. Who minted a key never decides it, because a minter can
be an admin who belongs to several partners, can have left, or the key can be deleted while its
organizations stay. These run the real queries on SQLite rows, in the shapes the reviews found.
"""

import pytest

from app.models.db_models import Organization, OrganizationCluster, OrganizationMember
from app.services import cluster_parties
from tests._sqlite_session import sqlite_session

A = "partner-a@example.test"
A2 = "partner-a2@example.test"
B = "partner-b@example.test"
X = "partner-x@example.test"
L = "local-partner@example.test"
L2 = "local-partner2@example.test"
ADMIN = "ops@example.test"
M = "dashboard-added@example.test"


def _org(org_id: int, slug: str, created_by: str, key: "str | None" = None, source: str = "orcahub") -> Organization:
    return Organization(id=org_id, name=slug, slug=slug, created_by=created_by, is_active=True,
                        source=source, api_key_id=key)


def _member(org_id: int, email: str, role: str = "partner") -> OrganizationMember:
    return OrganizationMember(organization_id=org_id, user_email=email, role=role)


def _bind(cluster: str, *org_ids: "int | None", registered_by: "str | None" = None) -> list:
    return [OrganizationCluster(organization_id=o, cluster_id=cluster, registered_by=registered_by) for o in org_ids]


async def _world():
    db = sqlite_session(Organization, OrganizationMember, OrganizationCluster)
    db.add_all([
        # A's home org, synced from OrcaHub on A's connection key, run by A and A2. The admin
        # account is a partner member too, as it is on the dev copy.
        _org(1, "a-home", "system", "oak_a"),
        _member(1, A), _member(1, A2), _member(1, ADMIN),
        # a partner added to A's home org from the dashboard, stored as the legacy "member" role
        _member(1, M, "member"),
        # a buyer A's key provisioned (provisioning stamps the minter), and one whose minter left
        _org(3, "a-buyer", A, "oak_a"),
        _org(7, "a-buyer-old-staff", "departed@example.test", "oak_a"),
        # B's home org on B's connection key, and a buyer of B
        _org(2, "b-home", "system", "oak_b"),
        _member(2, B),
        _org(4, "b-buyer", B, "oak_b"),
        # an org on a key that syncs nobody's home org here (deleted, rotated or admin-minted)
        _org(5, "orphan", "system", "oak_gone"),
        # X runs its own org and also buys from A as a tenant of A's buyer org
        _org(8, "x-home", "system", "oak_x"),
        _member(8, X), _member(3, X, "tenant"),
        # a partner whose home org is local, with a buyer its own key provisioned
        _org(9, "l-home", L, None, source="local"),
        _member(9, L), _member(9, L2),
        _org(10, "l-buyer", L, "oak_l"),
        # two partners whose orgs one admin created
        _org(12, "a-by-admin", ADMIN, None, source="local"),
        _org(13, "b-by-admin", ADMIN, None, source="local"),
        _member(12, A), _member(13, B),
    ])
    await db.commit()
    db.add_all([
        *_bind("sold", 1, 3, 7),
        *_bind("shared", 1, 2),
        *_bind("resold", 1, 4),
        *_bind("orphaned", 1, 5),
        *_bind("x-bought", 3, 8),
        *_bind("local-sold", 9, 10),
        *_bind("admin-shared", 12, 13),
        *_bind("squat", None, registered_by=B),
        *_bind("mine", None, registered_by=A),
    ])
    await db.commit()
    return db


async def held(db, email: str, *clusters: str) -> set:
    return await cluster_parties.clusters_held_by_another_party(db, email, set(clusters))


async def test_the_partners_own_buyers_do_not_make_its_cluster_foreign() -> None:
    db = await _world()
    assert await held(db, A, "sold") == set()
    assert await held(db, A2, "sold") == set(), "a teammate runs the same home org"


async def test_a_buyer_whose_minter_left_is_still_the_partners() -> None:
    db = await _world()
    assert await held(db, A2, "sold") == set()


async def test_another_partners_home_or_buyer_is_foreign() -> None:
    db = await _world()
    assert await held(db, A, "shared", "resold") == {"shared", "resold"}
    assert await held(db, B, "shared", "sold") == {"shared", "sold"}


async def test_an_org_on_a_key_that_syncs_none_of_the_callers_orgs_is_foreign() -> None:
    """A deleted, rotated or admin-minted key: none of it puts the org on anyone's side."""
    db = await _world()
    assert await held(db, A, "orphaned") == {"orphaned"}


async def test_an_admin_member_of_one_partner_does_not_join_the_partners() -> None:
    db = await _world()
    assert await held(db, A, "admin-shared") == {"admin-shared"}
    assert await held(db, B, "admin-shared") == {"admin-shared"}


async def test_buying_from_a_partner_does_not_join_the_buyer_to_the_seller() -> None:
    db = await _world()
    assert await held(db, X, "x-bought") == {"x-bought"}
    assert 3 not in await cluster_parties.run_orgs(db, X)


async def test_a_member_row_added_from_the_dashboard_does_not_run_the_org() -> None:
    db = await _world()
    assert 1 not in await cluster_parties.run_orgs(db, M)
    assert await held(db, M, "sold") == {"sold"}


async def test_a_local_home_partner_owns_the_buyers_it_created() -> None:
    db = await _world()
    assert await held(db, L, "local-sold") == set()


async def test_a_teammate_of_a_local_home_partner_is_narrowed_on_its_buyers() -> None:
    """Known limit, narrowing: the buyer carries a key no org L2 runs carries, and L2 did not create it."""
    db = await _world()
    assert await held(db, L2, "local-sold") == {"local-sold"}


async def test_unbound_rows_belong_to_whoever_registered_them() -> None:
    db = await _world()
    assert await held(db, A, "squat", "mine") == {"squat"}


async def test_the_keys_a_caller_runs_are_the_ones_their_orgs_carry() -> None:
    db = await _world()
    assert await cluster_parties.run_org_keys(db, A) == {"oak_a"}
    assert await cluster_parties.run_org_keys(db, X) == {"oak_x"}
    assert await cluster_parties.run_org_keys(db, "nobody@example.test") == set()


@pytest.mark.parametrize("email", ["", None])
async def test_no_email_runs_nothing(email) -> None:
    db = await _world()
    assert await cluster_parties.run_orgs(db, email) == {}
