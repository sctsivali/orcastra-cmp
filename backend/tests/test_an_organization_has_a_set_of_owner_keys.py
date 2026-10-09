"""A synced organization is owned by a set of keys, and its primary is always in it.

Every key in the set syncs the organization, writes its members and provisions for it; a key outside
the set stays refused exactly as before. A co-owner's push never moves the primary, which is the key
OrcaHub callbacks use. The migration that introduces the set gives every claimed organization its
current owner as the one row, so nothing behaves differently until a second key joins.

These send OrcaHub's bodies through the real external routes against SQLite rows, then read the
owner rows and the invariants every ownership change has to keep.
"""

import ast
import importlib.util
from pathlib import Path
from typing import Any, Dict, List

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, select, text

from app.models.db_models import Organization, OrganizationMember, OrganizationOwnerKey
from app.routers import integrations
from app.services import authentik_sync
from app.services.integration_key_ownership import transfer_owner
from app.services.integration_roles import ROLE_PARTNER, decide_upsert, membership_owners
from app.services.organization_owners import PARTY_ADDED_VIA, is_owner, owner_key_ids
from tests._owner_invariants import assert_owner_invariants
from tests._owner_key_world import PROVISION, SALE, SELLER, Directory, provision_body, send, world_db

APP = Path(__file__).resolve().parents[1] / "app"
K_SELLER, K_CO, K_OUT = "oak_seller", "oak_co_owner", "oak_outsider"
FOUNDER, MATE, BUYER_LOGIN = "founder@example.test", "mate@example.test", "buyer@example.test"
BUYER = "9e1f5d2a-4b6c-4c8d-8e0f-2a3b4c5d6e7f"


def key(key_id: str, **over: Any) -> Dict[str, Any]:
    return {"id": key_id, "app_name": "OrcaHub", "app_type": "orcahub", "created_by": "partner@example.test",
            "cluster_permissions": [{"cluster_id": SALE, "permissions": ["admin"], "projects": ["*"]}], **over}


def push(members: List[tuple] = ()) -> Dict[str, Any]:
    return {"include_clusters": False, "include_projects": False,
            "organizations": [{"id": SELLER, "name": "Acme Cloud", "slug": "acme-cloud"}],
            "members": [{"email": e, "name": None, "organization_id": SELLER, "role": r} for e, r in members]}


@pytest.fixture(autouse=True)
def directory(monkeypatch: pytest.MonkeyPatch) -> Directory:
    found = Directory({FOUNDER: {ROLE_PARTNER}})
    monkeypatch.setattr(authentik_sync, "get_authentik_sync", lambda: found)
    return found


async def org(db: Any, slug: str) -> Organization:
    return (await db.execute(select(Organization).where(Organization.slug == slug))).scalar_one()


async def co_own(db: Any, slug: str, key_id: str, via: str = "admin") -> None:
    """A second owner row, as an Administrator or an enrollment adds one."""
    db.add(OrganizationOwnerKey(organization_id=(await org(db, slug)).id, api_key_id=key_id, added_via=via,
                                added_by="admin@example.test"))
    await db.commit()


async def members_of(db: Any, slug: str) -> List[str]:
    target = await org(db, slug)
    rows = await db.execute(select(OrganizationMember.user_email).where(OrganizationMember.organization_id == target.id))
    return sorted(rows.scalars().all())


async def primary(db: Any, slug: str) -> str:
    return (await org(db, slug)).api_key_id


# note: the claim paths

async def test_every_claim_path_puts_its_key_in_the_set() -> None:
    db = world_db()
    assert (await send(key(K_SELLER), db, push([(FOUNDER, "PARTNER")]))).json()["organizations_synced"] == 1
    provisioned = await send(key(K_SELLER), db, provision_body(BUYER, [BUYER_LOGIN], name="Buyer Co", slug="buyer-co"),
                             path=PROVISION)
    assert provisioned.status_code == 200, provisioned.text
    via = dict((await db.execute(select(OrganizationOwnerKey.api_key_id, OrganizationOwnerKey.added_via)
                                 .join(Organization, Organization.id == OrganizationOwnerKey.organization_id)
                                 .where(Organization.slug == "buyer-co"))).all())
    assert via == {K_SELLER: "provision"}
    seller = await org(db, "acme-cloud")
    assert await owner_key_ids(db, seller.id) == [K_SELLER]
    (claim_via,) = (await db.execute(select(OrganizationOwnerKey.added_via)
                                     .where(OrganizationOwnerKey.organization_id == seller.id))).scalars().all()
    assert claim_via == "claim"
    await assert_owner_invariants(db)


async def test_claiming_an_existing_unclaimed_org_writes_its_row() -> None:
    """The listener's second path: an organization already in the database gets its first owner."""
    db = world_db()
    db.add(Organization(name="Acme Cloud", slug="acme-cloud", source="orcahub", external_id=SELLER,
                        api_key_id=None, created_by="system", is_active=True))
    await db.commit()
    assert await owner_key_ids(db, (await org(db, "acme-cloud")).id) == []
    assert (await send(key(K_SELLER), db, push([(FOUNDER, "PARTNER")]))).json()["organizations_synced"] == 1
    assert await primary(db, "acme-cloud") == K_SELLER
    assert await owner_key_ids(db, (await org(db, "acme-cloud")).id) == [K_SELLER]
    await assert_owner_invariants(db)


async def test_a_resync_by_the_owner_writes_no_second_row() -> None:
    db = world_db()
    for _ in range(3):
        await send(key(K_SELLER), db, push([(FOUNDER, "PARTNER")]))
    assert await owner_key_ids(db, (await org(db, "acme-cloud")).id) == [K_SELLER]
    await assert_owner_invariants(db)


# note: a co-owner, and a key outside the set

async def test_a_co_owner_syncs_without_moving_the_primary() -> None:
    db = world_db()
    await send(key(K_SELLER), db, push([(FOUNDER, "PARTNER")]))
    await co_own(db, "acme-cloud", K_CO)
    response = await send(key(K_CO), db, push([(FOUNDER, "PARTNER"), (MATE, "TENANT")]))
    assert response.json()["organizations_synced"] == 1
    assert await primary(db, "acme-cloud") == K_SELLER, "the primary stays the key that claimed it"
    assert await members_of(db, "acme-cloud") == sorted([FOUNDER, MATE])
    await assert_owner_invariants(db)


async def test_a_key_outside_the_set_stays_refused() -> None:
    db = world_db()
    await send(key(K_SELLER), db, push([(FOUNDER, "PARTNER")]))
    await co_own(db, "acme-cloud", K_CO)
    response = await send(key(K_OUT), db, push([("attacker@example.test", "PARTNER")]))
    assert response.status_code == 200 and response.json()["organizations_synced"] == 0
    assert response.json()["members_synced"] == 0
    assert await members_of(db, "acme-cloud") == [FOUNDER]
    assert not await is_owner(db, await org(db, "acme-cloud"), K_OUT)
    await assert_owner_invariants(db)


async def test_a_refused_organization_is_not_held_through_the_rest_of_the_sync() -> None:
    """A refusal ends its transaction, so the row read FOR UPDATE is not still locked when the sync goes
    on to take its key's lock (on PostgreSQL a transfer holding that lock and waiting on the row would
    deadlock with it)."""
    db = world_db()
    await send(key(K_SELLER), db, push())
    pushed = [integrations.SyncOrganization(id=SELLER, name="Acme Cloud", slug="acme-cloud")]
    assert await integrations._upsert_synced_organizations(db, pushed, K_OUT) == 0
    assert not db.sync.in_transaction(), "the refused organization's row is still held"
    assert await primary(db, "acme-cloud") == K_SELLER


async def test_a_slug_clash_is_not_held_through_the_rest_of_the_sync() -> None:
    db = world_db()
    db.add(Organization(name="Made Here", slug="made-here", source="local", created_by="partner@example.test",
                        is_active=True))
    await db.commit()
    pushed = [integrations.SyncOrganization(id="ext-new", name="Made Here", slug="made-here")]
    assert await integrations._upsert_synced_organizations(db, pushed, K_SELLER) == 0
    assert not db.sync.in_transaction(), "the clashing row is still held"


async def test_a_co_owner_provisions_and_an_outsider_does_not() -> None:
    db = world_db()
    await send(key(K_SELLER), db, push([(FOUNDER, "PARTNER")]))
    await co_own(db, "acme-cloud", K_CO)
    granted = await send(key(K_CO), db, provision_body(SELLER, [BUYER_LOGIN]), path=PROVISION)
    assert granted.status_code == 200, granted.text
    assert BUYER_LOGIN in await members_of(db, "acme-cloud")
    # note: another project, so the refusal is the owner set's and not the project holder's (403)
    refused = await send(key(K_OUT), db, provision_body(SELLER, ["x@example.test"], project_name="orc-p-2"),
                         path=PROVISION)
    assert refused.status_code == 409 and "owned by a different integration key" in refused.text
    same_project = await send(key(K_OUT), db, provision_body(SELLER, ["x@example.test"]), path=PROVISION)
    assert same_project.status_code == 403, "the project the co-owner provisioned is another party's to an outsider"
    assert await primary(db, "acme-cloud") == K_SELLER
    await assert_owner_invariants(db)


async def test_a_transfer_replaces_the_whole_set() -> None:
    db = world_db()
    await send(key(K_SELLER), db, push([(FOUNDER, "PARTNER")]))
    await co_own(db, "acme-cloud", K_CO)
    seller = await org(db, "acme-cloud")
    _org, previous = await transfer_owner(db, seller.id, K_OUT)
    await db.commit()
    assert previous == K_SELLER
    assert await owner_key_ids(db, seller.id) == [K_OUT]
    (via,) = (await db.execute(select(OrganizationOwnerKey.added_via)
                               .where(OrganizationOwnerKey.organization_id == seller.id))).scalars().all()
    assert via == "transfer"
    await assert_owner_invariants(db)


async def test_a_transfer_to_a_co_owner_restamps_its_row_as_a_transfer() -> None:
    """A transfer always lent the party; a co-owner that becomes the sole owner by one lends it too."""
    db = world_db()
    await send(key(K_SELLER), db, push([(FOUNDER, "PARTNER")]))
    await co_own(db, "acme-cloud", K_CO, via="enroll")
    seller = await org(db, "acme-cloud")
    await transfer_owner(db, seller.id, K_CO)
    await db.commit()
    rows = (await db.execute(select(OrganizationOwnerKey.api_key_id, OrganizationOwnerKey.added_via)
                             .where(OrganizationOwnerKey.organization_id == seller.id))).all()
    assert [tuple(r) for r in rows] == [(K_CO, "transfer")]
    await assert_owner_invariants(db)


# note: the party rules count only the rows that lend a party

def test_an_enrolled_or_admin_added_key_lends_no_party() -> None:
    assert set(PARTY_ADDED_VIA) == {"backfill", "claim", "provision", "transfer", "carry"}
    assert "enroll" not in PARTY_ADDED_VIA and "admin" not in PARTY_ADDED_VIA


async def test_a_co_owner_on_the_founders_buyer_does_not_put_it_on_the_co_owners_side() -> None:
    """The founder created the buyer; an Administrator added the seller's key to it. side_keys of the
    seller's minter is unchanged by that, so the buyer does not join the seller's party."""
    from app.services.cluster_parties import side_keys

    db = world_db()
    await send(key(K_SELLER), db, push([(FOUNDER, "PARTNER")]))
    db.add(Organization(name="Founder Co", slug="founder-co", source="orcahub", external_id="ext-founder",
                        api_key_id="oak_founders", created_by="partner@example.test", is_active=True))
    await db.commit()
    await co_own(db, "founder-co", K_SELLER)
    assert K_SELLER not in await side_keys(db, "partner@example.test") - {"oak_founders"}


# note: partner promotion reads every owner set

async def test_partner_promotion_reads_every_owner_set() -> None:
    db = world_db()
    await send(key(K_SELLER), db, push([(FOUNDER, "PARTNER")]))
    await co_own(db, "acme-cloud", K_CO)
    owners = await membership_owners(db, FOUNDER)
    assert owners == [frozenset({K_SELLER, K_CO})]
    assert decide_upsert("partner", set(), owners, K_CO).reason == "grant_partner"
    assert decide_upsert("partner", set(), owners, K_OUT).reason == "membership_outside_this_key"
    assert decide_upsert("partner", set(), [frozenset({K_CO}), frozenset()], K_CO).reason == (
        "membership_outside_this_key"
    )


# note: the migration

def _load_migration() -> Any:
    path = APP.parent / "alembic" / "versions" / "20261009_130000_015a_organization_owner_keys.py"
    spec = importlib.util.spec_from_file_location("migration_015a", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_branches_from_015_with_the_id_rc5_carries() -> None:
    migration = _load_migration()
    assert (migration.revision, migration.down_revision) == ("015a_organization_owner_keys", "015_session_audit_bigint")
    assert len(migration.revision) <= 32, "alembic_version.version_num is a varchar(32)"


def test_the_migration_backfills_every_claimed_org_and_comes_back_out() -> None:
    """015a on SQLite: one row per claimed organization, none for an unclaimed one, all gone on downgrade."""
    migration = _load_migration()
    engine = create_engine("sqlite://")
    Organization.__table__.create(engine)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO organizations (id, name, slug, created_by, is_active, source, external_id, api_key_id, "
            "created_at, updated_at) VALUES "
            "(1, 'A', 'a', 'system', 1, 'orcahub', 'x1', 'oak_a', '2026-01-01', '2026-01-01'), "
            "(2, 'B', 'b', 'system', 0, 'orcahub', 'x2', 'oak_b', '2026-01-02', '2026-01-02'), "
            "(3, 'L', 'l', 'p@example.test', 1, 'local', NULL, NULL, '2026-01-03', '2026-01-03')"
        ))
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        rows = conn.execute(text(
            "SELECT organization_id, api_key_id, added_via, added_by FROM organization_owner_keys ORDER BY organization_id"
        )).all()
        assert [tuple(r) for r in rows] == [(1, "oak_a", "backfill", "migration"), (2, "oak_b", "backfill", "migration")]
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
        assert "organization_owner_keys" not in inspect(conn).get_table_names()
        assert [tuple(r) for r in conn.execute(text("SELECT id, api_key_id FROM organizations ORDER BY id")).all()] == [
            (1, "oak_a"), (2, "oak_b"), (3, None),
        ], "the primary survives the downgrade"


# note: by shape, so a new writer or a comparison with the primary alone cannot ship unreviewed

# Every function that writes organizations.api_key_id, and why.
CLAIM_WRITERS = {
    "routers/integrations.py:_upsert_synced_organizations": "the sync and refresh claim of an unclaimed org",
    "routers/integrations.py:provision_tenant_access": "provisioning creates a buyer org with its key",
    "services/organization_owners.py:replace_owners": "an Administrator's transfer",
    "services/organization_owners.py:remove_owner": "a key leaving the set hands the primary to an able owner",
    "services/organization_owners.py:set_primary": "an Administrator choosing the primary within the set",
}
# Every function that builds an owner row.
OWNER_ROW_WRITERS = {
    "models/db_models.py:_keep_the_primary_in_the_owner_set": "the primary's row, in the flush that sets it",
    "services/organization_owners.py:add_owner": "an Administrator or an enrollment adding a co-owner",
}
# Every function comparing an api_key_id with ==/!=, and why it is not "the caller owns the org".
PRIMARY_COMPARISONS = {
    "models/db_models.py:_keep_the_primary_in_the_owner_set": "matches an owner row to the primary",
    "routers/image_uploads.py:assert_session_owner": "an upload session's own key, not an organization",
    "routers/image_uploads.py:create_upload": "an upload session's own key, not an organization",
    "routers/iso_uploads.py:create_upload": "an upload session's own key, not an organization",
    "routers/integrations.py:_refuse_a_project_another_party_holds": "compares owner rows",
    "routers/integrations.py:set_organization_primary": "a primary that is already the one asked for is a no-op",
    "services/integration_key_ownership.py:refuse_if_key_owns_organizations": "the other rows of a set",
    "services/orcahub_sync.py:_get_orcahub_credentials": "the callback fallback past the primary",
    "services/organization_owners.py:choose_primary": "skips the ending key among owner rows",
    "services/organization_owners.py:remove_owner": "the ending key among owner rows",
    "services/cluster_parties.py:home_partner_keys": "joins owner rows to owner rows",
    "services/organization_owners.py:is_owner": "the owner test itself",
    "services/organization_owners.py:owned_org_ids": "owner rows of one key",
    "services/organization_owners.py:replace_owners": "the transfer itself",
}


def _scan() -> Dict[str, Dict[str, list]]:
    found: Dict[str, Dict[str, list]] = {"writes": {}, "rows": {}, "compares": {}}
    for path in sorted(APP.rglob("*.py")):
        for fn in ast.walk(ast.parse(path.read_text())):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            where = f"{path.relative_to(APP)}:{fn.name}"
            for node in ast.walk(fn):
                if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Attribute) and t.attr == "api_key_id" for t in node.targets
                ):
                    found["writes"].setdefault(where, []).append(node.lineno)
                if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Organization" and any(
                    k.arg == "api_key_id" for k in node.keywords
                ):
                    found["writes"].setdefault(where, []).append(node.lineno)
                if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "OrganizationOwnerKey":
                    found["rows"].setdefault(where, []).append(node.lineno)
                if isinstance(node, ast.Compare) and any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops):
                    if any(isinstance(x, ast.Attribute) and x.attr == "api_key_id" for x in [node.left, *node.comparators]):
                        found["compares"].setdefault(where, []).append(node.lineno)
    return found


def test_only_these_paths_write_a_claim() -> None:
    assert set(_scan()["writes"]) == set(CLAIM_WRITERS)


def test_only_the_owner_module_writes_owner_rows() -> None:
    assert set(_scan()["rows"]) == set(OWNER_ROW_WRITERS)


def test_no_ownership_by_primary_equality() -> None:
    assert set(_scan()["compares"]) == set(PRIMARY_COMPARISONS)
