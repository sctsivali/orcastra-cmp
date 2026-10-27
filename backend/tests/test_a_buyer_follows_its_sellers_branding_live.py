"""A buyer shows its seller's branding as it is now, not as it was on the day of the sale.

Provisioning used to copy the seller's row into the buyer once (a partner rebranded and
none of its tenants saw it). The buyer now carries a link (organizations.provider_organization_id)
and the layers are read on every request (services/branding/store.py).
"""

from __future__ import annotations

import pytest

from app.models.branding_models import DeploymentBranding
from app.models.db_models import Organization, OrganizationMember, OrganizationPreferences, TenantAccessPolicy
from app.services.branding import store
from app.services.branding.validation import BrandingInvalid
from tests._sqlite_session import org_tables, sqlite_session, with_json_tables

SELLER, BUYER = 5, 9
TENANT = "tenant@buyer.test"


@pytest.fixture
def audits(monkeypatch):
    events = []

    class _Logger:
        def log_activity(self, **kwargs):
            events.append(kwargs)

    monkeypatch.setattr(store, "get_activity_logger", lambda: _Logger())
    return events


async def _world():
    db = with_json_tables(
        sqlite_session(*org_tables()), OrganizationPreferences, DeploymentBranding, TenantAccessPolicy,
    )
    db.add_all([
        Organization(id=SELLER, name="Seller Cloud", slug="seller-cloud", created_by="ops@eco.test", source="local"),
        Organization(id=BUYER, name="Buyer", slug="buyer", created_by="ops@eco.test", source="orcahub",
                     provider_organization_id=SELLER, provider_link_source="provision"),
    ])
    await db.commit()
    db.add_all([
        OrganizationMember(organization_id=BUYER, user_email=TENANT, role="tenant"),
        OrganizationPreferences(organization_id=SELLER, settings={"branding": {"appName": "Seller Cloud"}},
                                updated_by="ops@eco.test"),
    ])
    await db.commit()
    return db


async def test_a_tenant_of_the_buyer_sees_the_sellers_branding() -> None:
    db = await _world()
    account = await store.account_branding(db, TENANT, admin_only=False)

    assert account.organization_id == BUYER
    assert account.resolved.source == "provider"
    assert account.resolved.settings["branding"]["appName"] == "Seller Cloud"


async def test_a_change_by_the_seller_reaches_the_buyer_and_moves_its_version(audits) -> None:
    db = await _world()
    before = await store.account_branding(db, TENANT, admin_only=False, with_settings=False)

    await store.save_org_branding(db, SELLER, {"branding": {"appName": "Seller Cloud Plus"}}, "ops@eco.test")

    after = await store.account_branding(db, TENANT, admin_only=False)
    assert after.resolved.settings["branding"]["appName"] == "Seller Cloud Plus"
    assert after.resolved.version != before.resolved.version
    assert audits[-1]["action"] == "organization.branding.update"
    assert audits[-1]["target_organization_id"] == SELLER


async def test_the_version_read_never_loads_the_settings() -> None:
    db = await _world()
    full = await store.org_branding(db, BUYER)
    light = await store.org_branding(db, BUYER, with_settings=False)

    assert light.version == full.version
    rendered = " ".join(str(statement) for statement in db.statements[-3:])
    assert "settings" not in rendered.split("FROM")[0], "the poll selected the settings column"


async def test_the_buyers_own_row_wins_and_a_reset_follows_the_seller_again(audits) -> None:
    db = await _world()
    await store.save_org_branding(db, BUYER, {"branding": {"appName": "Buyer Co"}}, "founder@buyer.test")
    assert (await store.org_branding(db, BUYER)).settings["branding"]["appName"] == "Buyer Co"

    assert await store.reset_org_branding(db, BUYER) is True
    resolved = await store.org_branding(db, BUYER)
    assert resolved.settings["branding"]["appName"] == "Seller Cloud"
    assert await store.own_row(db, BUYER) is None, "a reset deletes the row instead of storing {}"
    assert [event["action"] for event in audits] == ["organization.branding.update", "organization.branding.reset"]


async def test_a_deactivated_seller_stops_lending_its_look() -> None:
    db = await _world()
    seller = await db.get(Organization, SELLER)
    seller.is_active = False
    await db.commit()

    assert (await store.org_branding(db, BUYER)).source == "default"


async def test_the_link_is_one_hop_never_the_providers_provider() -> None:
    db = await _world()
    db.add(Organization(id=1, name="Upstream", slug="upstream", created_by="x", source="local"))
    await db.commit()
    db.add(OrganizationPreferences(organization_id=1, settings={"theme": "dark"}, updated_by="x"))
    seller = await db.get(Organization, SELLER)
    seller.provider_organization_id = 1
    await db.commit()

    assert "theme" not in (await store.org_branding(db, BUYER)).settings


async def test_an_invalid_write_changes_nothing(audits) -> None:
    db = await _world()
    with pytest.raises(BrandingInvalid):
        await store.save_org_branding(db, BUYER, {"colors": {"accentColor": "red"}}, "founder@buyer.test")

    assert await store.own_row(db, BUYER) is None
    assert audits == []


async def test_the_deployment_default_is_audited_on_write_and_reset(audits) -> None:
    db = await _world()
    await store.save_deployment_branding(db, {"branding": {"appName": "Acme"}}, "admin@acme.test")
    assert (await store.org_branding(db, BUYER)).settings["branding"]["appName"] == "Seller Cloud"
    assert await store.reset_deployment_branding(db) is True
    assert [event["action"] for event in audits] == ["deployment.branding.update", "deployment.branding.reset"]
