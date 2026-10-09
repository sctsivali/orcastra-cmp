"""A key another owner enrolled, or an Administrator added, owns the organization but lends no party.

Who manages a key (read, revoke, delete, capabilities, rotate-secret) is decided by party
(services/cluster_parties.py), and the party of an organization is the integration key it carries.
Counting every owner key there let the seller who provisioned a buyer manage, and rotate to receive
the secret of, a key the buyer's founder enrolled into that buyer. Only an owner row that came from a
claim, provisioning, a transfer or a carry of one of those lends a party, as before co-ownership.
"""

from typing import Any, Dict

import pytest
from fastapi import HTTPException

from app.core import org_scope
from app.core.auth import AuthenticatedUser
from app.models.db_models import Organization, OrganizationCluster, OrganizationMember, OrganizationOwnerKey
from app.routers import integrations
from app.services import cluster_parties
from app.services.organization_owners import add_owner, carry, remove_owner, replace_owners
from tests._owner_invariants import assert_owner_invariants
from tests._sqlite_session import org_tables, sqlite_session

SELLER = "seller@example.test"
FOUNDER = "founder@example.test"
SELLER_KEY = "oak_seller"
FOUNDER_KEY = "oak_founder"
CLUSTER = "cluster-sold"


def _org(db: Any, slug: str, created_by: str, key_id: str) -> Organization:
    org = Organization(name=slug.title(), slug=slug, source="orcahub", external_id=f"ext-{slug}",
                       api_key_id=key_id, created_by=created_by, is_active=True)
    db.add(org)
    db.sync.commit()
    return org


@pytest.fixture
def world() -> Dict[str, Any]:
    """The seller's home and a buyer its key provisioned, both on CLUSTER, and the founder's own org there too."""
    db = sqlite_session(*org_tables())
    home = _org(db, "seller-home", "system", SELLER_KEY)
    buyer = _org(db, "buyer", SELLER, SELLER_KEY)
    own = _org(db, "founder-own", "system", FOUNDER_KEY)
    db.add_all([
        OrganizationMember(organization_id=home.id, user_email=SELLER, role="partner"),
        OrganizationMember(organization_id=buyer.id, user_email=FOUNDER, role="partner"),
        OrganizationMember(organization_id=own.id, user_email=FOUNDER, role="partner"),
        OrganizationCluster(organization_id=home.id, cluster_id=CLUSTER, registered_by=SELLER),
        OrganizationCluster(organization_id=buyer.id, cluster_id=CLUSTER, registered_by=None),
        OrganizationCluster(organization_id=own.id, cluster_id=f"{CLUSTER}-own", registered_by=FOUNDER),
    ])
    db.sync.commit()
    # note: the founder's org shares the cluster through a second binding row of its own
    db.add(OrganizationCluster(organization_id=own.id, cluster_id=CLUSTER, registered_by=FOUNDER))
    db.sync.commit()
    return {"db": db, "home": home, "buyer": buyer, "own": own}


def _seller() -> AuthenticatedUser:
    return AuthenticatedUser(user_id="partner:seller", email=SELLER, groups=["role_partner"])


def _founder_key() -> Dict[str, Any]:
    return {
        "id": FOUNDER_KEY, "name": FOUNDER_KEY, "app_name": "orcahub", "app_type": "orcahub",
        "created_at": "2026-09-01T00:00:00", "created_by": FOUNDER, "is_active": True,
        "capabilities": ["inventory.read", "org.sync", "tenant.provision"],
        "cluster_permissions": [{"cluster_id": CLUSTER, "permissions": ["read", "write"], "projects": ["*"]}],
    }


@pytest.mark.parametrize("via", ["enroll", "admin"])
async def test_an_enrolled_co_owner_is_not_the_sellers_to_manage(world: Dict[str, Any], via: str) -> None:
    db = world["db"]
    assert await add_owner(db, world["buyer"], FOUNDER_KEY, via=via, by="key:oak_seller", proof_key_id=SELLER_KEY)
    await db.commit()
    assert FOUNDER_KEY not in await cluster_parties.side_keys(db, SELLER)
    assert await cluster_parties.clusters_held_by_another_party(db, SELLER, {CLUSTER}) == {CLUSTER}
    assert await integrations._keys_the_caller_manages(db, _seller(), [_founder_key()]) == []
    await assert_owner_invariants(db)


@pytest.mark.parametrize("handler", [
    lambda user, db: integrations.rotate_api_key_secret(FOUNDER_KEY, None, user, db),
    lambda user, db: integrations.revoke_api_key(FOUNDER_KEY, None, user, db),
    lambda user, db: integrations.get_api_key(FOUNDER_KEY, user=user, db=db),
], ids=["rotate", "revoke", "read"])
async def test_the_seller_cannot_rotate_or_revoke_an_enrolled_key(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, handler: Any
) -> None:
    db = world["db"]
    assert await add_owner(db, world["buyer"], FOUNDER_KEY, via="enroll", by="key:oak_seller", proof_key_id=SELLER_KEY)
    await db.commit()
    monkeypatch.setattr(integrations, "_read_api_key_metadata_blocking", lambda _k: _founder_key())
    touched = []
    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: touched.append("vault") or object())
    token = org_scope._allowed_clusters_var.set({CLUSTER})
    try:
        with pytest.raises(HTTPException) as caught:
            await handler(_seller(), db)
    finally:
        org_scope._allowed_clusters_var.reset(token)
    assert caught.value.status_code == 404
    assert touched == [], "the handler went on to Vault for a key the seller does not manage"


async def test_a_transfer_still_puts_the_organization_on_the_new_keys_side(world: Dict[str, Any]) -> None:
    """The single-owner semantics: an Administrator's transfer moves the party with the organization."""
    db = world["db"]
    assert await add_owner(db, world["buyer"], FOUNDER_KEY, via="enroll", by="key:oak_seller", proof_key_id=SELLER_KEY)
    await db.commit()
    assert await replace_owners(db, world["buyer"], FOUNDER_KEY, via="transfer", by="admin@example.test") == [SELLER_KEY]
    await db.commit()
    row = db.sync.query(OrganizationOwnerKey).filter_by(organization_id=world["buyer"].id).one()
    assert (row.api_key_id, row.added_via, row.added_by) == (FOUNDER_KEY, "transfer", "admin@example.test")
    assert FOUNDER_KEY in await cluster_parties.side_keys(db, SELLER), "the seller created the buyer"
    await assert_owner_invariants(db)


@pytest.mark.parametrize("primary", [True, False], ids=["primary", "co-owner"])
async def test_a_carried_enrolled_key_still_lends_no_party(world: Dict[str, Any], primary: bool) -> None:
    db = world["db"]
    buyer = world["buyer"]
    assert await add_owner(db, buyer, FOUNDER_KEY, via="enroll", by="key:oak_seller", proof_key_id=SELLER_KEY)
    await db.commit()
    if primary:
        buyer.api_key_id = FOUNDER_KEY
        await db.commit()
    await carry(db, buyer, from_key_id=FOUNDER_KEY, to_key_id="oak_founder_next")
    await db.commit()
    row = db.sync.query(OrganizationOwnerKey).filter_by(organization_id=buyer.id, api_key_id="oak_founder_next").one()
    assert row.added_via == "enroll"
    assert "oak_founder_next" not in await cluster_parties.side_keys(db, SELLER)
    await assert_owner_invariants(db)


async def test_a_promoted_enrolled_key_still_lends_no_party(world: Dict[str, Any]) -> None:
    """Pinned on purpose: when the claiming key leaves, the enrolled key becomes primary as it came in.

    The organization then lends no party, so its partners no longer manage keys on a shared cluster
    through it, until an Administrator's transfer restamps the row. Fail-closed: it only narrows access.
    """
    db = world["db"]
    buyer = world["buyer"]
    assert await add_owner(db, buyer, FOUNDER_KEY, via="enroll", by="key:oak_seller", proof_key_id=SELLER_KEY)
    await db.commit()
    assert await remove_owner(db, buyer, SELLER_KEY, able=lambda _key: True) == FOUNDER_KEY
    await db.commit()
    row = db.sync.query(OrganizationOwnerKey).filter_by(organization_id=buyer.id).one()
    assert (row.api_key_id, row.added_via) == (FOUNDER_KEY, "enroll")
    assert FOUNDER_KEY not in await cluster_parties.side_keys(db, SELLER)
    await assert_owner_invariants(db)
