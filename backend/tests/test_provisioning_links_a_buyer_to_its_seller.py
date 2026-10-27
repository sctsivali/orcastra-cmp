"""Provisioning links a buyer to the organization that sold to it, and never overrides a decision.

The seller is the organization holding the binding its own member registered on the sold cluster,
the fact the proof of sale reads (services/cluster_parties.py). When the cluster says nothing, the
key's minter decides, only if it runs exactly one organization. An Administrator's choice, "none"
included, stays (services/branding/provider_link.py).
"""

from __future__ import annotations

import pytest

from app.models.db_models import Organization, OrganizationCluster, OrganizationMember
from app.services.branding import provider_link
from tests._sqlite_session import org_tables, sqlite_session

SELLER, OTHER, BUYER = 5, 6, 9


@pytest.fixture(autouse=True)
def quiet_audit(monkeypatch):
    class _Logger:
        def log_activity(self, **_kwargs):
            pass

    monkeypatch.setattr(provider_link, "get_activity_logger", lambda: _Logger())


async def _world(*, seller_registers: bool = True, other_registers: bool = False, buyer_source: str = "admin"):
    db = sqlite_session(*org_tables())
    db.add_all([
        Organization(id=SELLER, name="Seller", slug="seller", created_by="ops@seller.test", source="local"),
        Organization(id=OTHER, name="Other", slug="other", created_by="ops@other.test", source="local"),
        Organization(id=BUYER, name="Buyer", slug="buyer", created_by="ops@seller.test", source="orcahub",
                     provider_link_source=None if buyer_source == "admin" else buyer_source),
    ])
    await db.commit()
    db.add_all([
        OrganizationMember(organization_id=SELLER, user_email="ops@seller.test", role="member"),
        OrganizationMember(organization_id=OTHER, user_email="ops@other.test", role="member"),
        OrganizationCluster(organization_id=BUYER, cluster_id="sold-01", registered_by=None),
    ])
    if seller_registers:
        db.add(OrganizationCluster(organization_id=SELLER, cluster_id="sold-01", registered_by="OPS@seller.test"))
    if other_registers:
        db.add(OrganizationCluster(organization_id=OTHER, cluster_id="sold-01", registered_by="ops@other.test"))
    await db.commit()
    return db


async def _link(db, minter="ops@seller.test"):
    buyer = await db.get(Organization, BUYER)
    linked = await provider_link.link_on_provision(db, buyer, cluster_id="sold-01", key_minter=minter, api_key_id="k1")
    await db.commit()
    return linked, await db.get(Organization, BUYER)


async def test_the_organization_that_registered_the_sold_cluster_becomes_the_provider() -> None:
    linked, buyer = await _link(await _world())

    assert linked == SELLER
    assert (buyer.provider_organization_id, buyer.provider_link_source, buyer.provider_linked_by) == (
        SELLER, "provision", "key:k1",
    )


async def test_two_registrants_on_the_cluster_leave_the_buyer_unlinked() -> None:
    linked, buyer = await _link(await _world(other_registers=True))

    assert linked is None
    assert buyer.provider_organization_id is None
    assert buyer.provider_link_source is None, "left open for a later provision or an Administrator"


async def test_without_a_registrant_the_minter_decides_when_it_runs_one_organization() -> None:
    linked, _buyer = await _link(await _world(seller_registers=False))
    assert linked == SELLER


async def test_a_minter_running_several_organizations_decides_nothing() -> None:
    db = await _world(seller_registers=False)
    db.add(OrganizationMember(organization_id=OTHER, user_email="ops@seller.test", role="partner"))
    await db.commit()

    linked, _buyer = await _link(db)
    assert linked is None


async def test_an_administrators_choice_is_never_overwritten() -> None:
    db = await _world()
    buyer = await db.get(Organization, BUYER)
    await provider_link.set_provider(db, buyer, None, "admin@x.test")

    linked, buyer = await _link(db)
    assert linked is None
    assert (buyer.provider_organization_id, buyer.provider_link_source) == (None, "admin")


async def test_an_earlier_link_is_kept() -> None:
    db = await _world(other_registers=False)
    buyer = await db.get(Organization, BUYER)
    buyer.provider_organization_id, buyer.provider_link_source = OTHER, "migration"
    await db.commit()

    linked, buyer = await _link(db)
    assert (linked, buyer.provider_organization_id) == (OTHER, OTHER)


async def test_a_failure_inside_the_link_never_fails_the_sale(monkeypatch) -> None:
    db = await _world()

    async def boom(*_args, **_kwargs):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(provider_link, "seller_of", boom)
    linked, buyer = await _link(db)
    assert linked is None
    assert buyer.provider_organization_id is None


async def test_an_administrator_cannot_make_an_organization_its_own_provider() -> None:
    db = await _world()
    buyer = await db.get(Organization, BUYER)
    with pytest.raises(provider_link.ProviderInvalid):
        await provider_link.set_provider(db, buyer, BUYER, "admin@x.test")
    with pytest.raises(provider_link.ProviderInvalid):
        await provider_link.set_provider(db, buyer, 404, "admin@x.test")
