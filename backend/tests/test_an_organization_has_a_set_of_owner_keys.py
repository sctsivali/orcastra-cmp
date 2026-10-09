"""A synced organization is owned by a set of keys, and its primary is always in it.

Every key in the set syncs the organization, writes its members and provisions for it; a key outside
the set stays refused exactly as before. A co-owner's push never moves the primary, which is the key
OrcaHub callbacks use. The migration that introduces the set gives every claimed organization its
current owner as the one row, so nothing behaves differently until a second key joins.
"""

import importlib.util
from pathlib import Path
from typing import Any, Dict

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, select, text

from app.models.db_models import Organization, OrganizationMember, OrganizationOwnerKey
from app.routers import integrations
from app.services.integration_key_ownership import carry_owned_organizations, transfer_owner
from app.services.integration_roles import decide_upsert, membership_owners
from app.services.organization_owners import (
    LastOwner,
    MAX_OWNER_KEYS,
    NoAbleOwner,
    OwnerSetFull,
    add_owner,
    is_owner,
    owner_key_ids,
    remove_owner,
)
from tests._owner_invariants import assert_owner_invariants
from tests.test_a_push_rebinds_only_into_the_org_its_key_claimed import K_ATTACKER, K_PARTNER
from tests.test_the_one_org_rule_holds_for_orcahubs_bodies_and_every_claim_path import (
    BUYER1,
    BUYER_LOGIN,
    K_NEW,
    SELLER,
    claims,
    provision,
    provision_body,
    sale,  # noqa: F401
    sync,
    world_db,
)
from tests.test_the_one_org_rule_holds_for_orcahubs_bodies_and_every_claim_path import hub_body, seller_key
from tests.test_the_one_org_rule_holds_through_the_sync_route import (  # noqa: F401
    C,
    F,
    hub,
    send,
    world,
)


async def org(db: Any, slug: str) -> Organization:
    return (await db.execute(select(Organization).where(Organization.slug == slug))).scalar_one()


async def co_own(db: Any, slug: str, key_id: str) -> None:
    assert await add_owner(db, await org(db, slug), key_id, via="admin", by="admin@example.test")
    await db.commit()


async def members_of(db: Any, slug: str) -> list:
    target = await org(db, slug)
    rows = await db.execute(select(OrganizationMember.user_email).where(OrganizationMember.organization_id == target.id))
    return sorted(rows.scalars().all())


async def test_every_claim_path_puts_its_key_in_the_set(sale: Dict[str, Any]) -> None:  # noqa: F811
    db = world_db()
    await sync(db, (SELLER,), [(F, SELLER, "PARTNER")])
    assert (await provision(db, provision_body(BUYER1, [BUYER_LOGIN]))).status_code == 200
    via = dict((await db.execute(select(OrganizationOwnerKey.api_key_id, OrganizationOwnerKey.added_via)
                                 .join(Organization, Organization.id == OrganizationOwnerKey.organization_id)
                                 .where(Organization.slug == "buyer-co"))).all())
    assert via == {K_PARTNER: "provision"}
    assert await owner_key_ids(db, (await org(db, "acme-cloud")).id) == [K_PARTNER]
    await assert_owner_invariants(db)


async def test_a_co_owner_syncs_without_moving_the_primary(hub: Dict[str, Any]) -> None:  # noqa: F811
    db = world_db()
    await sync(db, (SELLER,), [(F, SELLER, "PARTNER")])
    await co_own(db, "acme-cloud", K_NEW)
    response = await sync(db, (SELLER,), [(F, SELLER, "PARTNER"), (C, SELLER, "PARTNER")], key_id=K_NEW)
    assert response.json()["organizations_synced"] == 1
    assert await claims(db) == [("acme-cloud", K_PARTNER)], "the primary stays the key that claimed it"
    assert await members_of(db, "acme-cloud") == sorted([C, F])
    await assert_owner_invariants(db)


async def test_a_key_outside_the_set_stays_refused(hub: Dict[str, Any]) -> None:  # noqa: F811
    db = world_db()
    await sync(db, (SELLER,), [(F, SELLER, "PARTNER")])
    await co_own(db, "acme-cloud", K_NEW)
    response = await send(seller_key(K_ATTACKER), db, hub_body((SELLER,), [("attacker@example.test", SELLER, "PARTNER")]))
    assert response.status_code == 200 and response.json()["organizations_synced"] == 0
    assert await members_of(db, "acme-cloud") == [F]
    assert not await is_owner(db, await org(db, "acme-cloud"), K_ATTACKER)


MATE = "mate@example.test"


def moved_meanwhile(db: Any, to_key: str) -> Organization:
    """acme-cloud as this session holds it, after its owner set moved to `to_key` outside its view."""
    held = db.sync.execute(select(Organization).where(Organization.slug == "acme-cloud")).scalar_one()
    db.sync.execute(text("UPDATE organizations SET api_key_id = :k WHERE slug = 'acme-cloud'"), {"k": to_key})
    db.sync.execute(text("UPDATE organization_owner_keys SET api_key_id = :k"), {"k": to_key})
    db.sync.commit()
    assert held.api_key_id == K_PARTNER, "precondition: the session still holds the old primary"
    return held


async def test_member_writes_judge_the_organization_as_it_stands_now(hub: Dict[str, Any]) -> None:  # noqa: F811
    """A transfer that lands while a long member list is written: the key it took the organization from
    writes no member there, though the session still holds that key as the primary."""
    db = world_db()
    await sync(db, (SELLER,), [(F, SELLER, "PARTNER")])
    # note: held, as the member loop holds the organization it wrote the previous member into
    held = moved_meanwhile(db, K_ATTACKER)
    pushed = [integrations.SyncMember(email=MATE, name=None, organization_id=SELLER, role="TENANT")]
    assert await integrations._upsert_synced_members(db, pushed, K_PARTNER) == (0, 0)
    assert await members_of(db, "acme-cloud") == [F]
    assert held.api_key_id == K_ATTACKER, "the lookup read the organization again"


async def test_reconcile_judges_the_organization_as_it_stands_now(hub: Dict[str, Any]) -> None:  # noqa: F811
    """The same move ahead of a reconcile. The reconcile asks the owner rows themselves (owner_key_ids),
    never the session's copy of the primary, so the key it took the organization from prunes nothing."""
    db = world_db()
    await sync(db, (SELLER,), [(F, SELLER, "PARTNER")])
    moved_meanwhile(db, K_ATTACKER)
    pushed = [integrations.SyncOrganization(id=SELLER, name="Acme Cloud", slug="acme-cloud")]
    # note: a roster without F, so only the ownership check stands between F and its removal
    roster = [integrations.SyncMember(email=MATE, name=None, organization_id=SELLER, role="TENANT")]
    assert await integrations._reconcile_synced_members(db, pushed, roster, K_PARTNER, dry_run=False) == 0
    assert await members_of(db, "acme-cloud") == [F]


async def test_a_co_owner_provisions_and_an_outsider_does_not(sale: Dict[str, Any]) -> None:  # noqa: F811
    db = world_db()
    await sync(db, (SELLER,), [(F, SELLER, "PARTNER")])
    await co_own(db, "acme-cloud", K_NEW)
    assert (await provision(db, provision_body(SELLER, [BUYER_LOGIN]), key_id=K_NEW)).status_code == 200
    assert BUYER_LOGIN in await members_of(db, "acme-cloud")
    refused = await provision(db, provision_body(SELLER, ["x@example.test"]), key_id=K_ATTACKER)
    assert refused.status_code == 409 and "owned by a different integration key" in refused.text
    await assert_owner_invariants(db)


async def test_a_transfer_replaces_the_whole_set(hub: Dict[str, Any]) -> None:  # noqa: F811
    db = world_db()
    await sync(db, (SELLER,), [(F, SELLER, "PARTNER")])
    await co_own(db, "acme-cloud", K_NEW)
    seller = await org(db, "acme-cloud")
    _org, previous = await transfer_owner(db, seller.id, K_ATTACKER)
    await db.commit()
    assert previous == K_PARTNER
    assert await owner_key_ids(db, seller.id) == [K_ATTACKER]
    await assert_owner_invariants(db)


async def test_a_carry_swaps_the_key_and_keeps_the_primary(hub: Dict[str, Any]) -> None:  # noqa: F811
    db = world_db()
    await sync(db, (SELLER,), [(F, SELLER, "PARTNER")])
    await co_own(db, "acme-cloud", K_NEW)
    carried = await carry_owned_organizations(db, from_key_id=K_NEW, to_key_id="oak_rotated")
    await db.commit()
    assert [slug for _id, slug, _ext in carried] == ["acme-cloud"]
    assert sorted(await owner_key_ids(db, (await org(db, "acme-cloud")).id)) == sorted([K_PARTNER, "oak_rotated"])
    assert await claims(db) == [("acme-cloud", K_PARTNER)]
    carried = await carry_owned_organizations(db, from_key_id=K_PARTNER, to_key_id="oak_rotated_primary")
    await db.commit()
    assert await claims(db) == [("acme-cloud", "oak_rotated_primary")]
    await assert_owner_invariants(db)


async def test_removal_promotes_the_oldest_able_owner_and_never_empties_the_set(hub: Dict[str, Any]) -> None:  # noqa: F811
    db = world_db()
    await sync(db, (SELLER,), [(F, SELLER, "PARTNER")])
    await co_own(db, "acme-cloud", K_NEW)
    await co_own(db, "acme-cloud", "oak_third")
    seller = await org(db, "acme-cloud")
    with pytest.raises(LookupError):
        await remove_owner(db, seller, K_PARTNER, able=lambda key: None)
    with pytest.raises(NoAbleOwner):
        await remove_owner(db, seller, K_PARTNER, able=lambda key: False)
    assert await remove_owner(db, seller, K_PARTNER, able=lambda key: key == "oak_third") == "oak_third"
    await db.commit()
    assert sorted(await owner_key_ids(db, seller.id)) == sorted([K_NEW, "oak_third"])
    assert await remove_owner(db, seller, K_NEW, able=lambda key: True) == "oak_third"
    await db.commit()
    with pytest.raises(LastOwner):
        await remove_owner(db, seller, "oak_third", able=lambda key: True)
    assert await remove_owner(db, seller, "oak_never", able=lambda key: True) is None
    await assert_owner_invariants(db)


async def test_the_set_is_capped(hub: Dict[str, Any]) -> None:  # noqa: F811
    db = world_db()
    await sync(db, (SELLER,), [(F, SELLER, "PARTNER")])
    for n in range(MAX_OWNER_KEYS - 1):
        await co_own(db, "acme-cloud", f"oak_extra_{n}")
    with pytest.raises(OwnerSetFull):
        await add_owner(db, await org(db, "acme-cloud"), "oak_one_too_many", via="admin", by="admin@example.test")
    assert not await add_owner(db, await org(db, "acme-cloud"), K_PARTNER, via="admin", by="a@example.test")


async def test_partner_promotion_reads_every_owner_set(hub: Dict[str, Any]) -> None:  # noqa: F811
    db = world_db()
    await sync(db, (SELLER,), [(F, SELLER, "PARTNER")])
    await co_own(db, "acme-cloud", K_NEW)
    owners = await membership_owners(db, F)
    assert owners == [frozenset({K_PARTNER, K_NEW})]
    assert decide_upsert("partner", set(), owners, K_NEW).reason == "grant_partner"
    assert decide_upsert("partner", set(), owners, K_ATTACKER).reason == "membership_outside_this_key"
    assert decide_upsert("partner", set(), [frozenset({K_NEW}), frozenset()], K_NEW).reason == "membership_outside_this_key"


def test_the_migration_backfills_every_claimed_org_and_comes_back_out() -> None:
    """015a on SQLite: one row per claimed organization, none for an unclaimed one, all gone on downgrade."""
    migration = _load_migration()
    engine = create_engine("sqlite://")
    Organization.__table__.create(engine)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO organizations (id, name, slug, created_by, is_active, source, external_id, api_key_id, created_at, updated_at) VALUES "
            "(1, 'A', 'a', 'system', 1, 'orcahub', 'x1', 'oak_a', '2026-01-01', '2026-01-01'), "
            "(2, 'B', 'b', 'system', 0, 'orcahub', 'x2', 'oak_b', '2026-01-02', '2026-01-02'), "
            "(3, 'L', 'l', 'p@example.test', 1, 'local', NULL, NULL, '2026-01-03', '2026-01-03')"
        ))
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        rows = conn.execute(text(
            "SELECT organization_id, api_key_id, added_via FROM organization_owner_keys ORDER BY organization_id"
        )).all()
        assert [tuple(r) for r in rows] == [(1, "oak_a", "backfill"), (2, "oak_b", "backfill")]
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
        assert "organization_owner_keys" not in inspect(conn).get_table_names()


def _load_migration() -> Any:
    # note: 015a, the hotfix line's revision, creates the set; 038 only merges it into this chain
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "20261009_130000_015a_organization_owner_keys.py"
    spec = importlib.util.spec_from_file_location("migration_015a", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
