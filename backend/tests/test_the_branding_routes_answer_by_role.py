"""The organization branding routes: who reads what, and what a write must look like
(routers/organization_branding.py).

A tenant reads what it sees, never who edited it or which organization lends it the look (on a
buyer that names the seller's staff). A partner editing an organization sees what it inherits
and how many organizations follow it. An invalid write is refused before anything is stored.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException, Response

from app.core.auth import AuthenticatedUser
from app.models.branding_models import DeploymentBranding
from app.models.db_models import (
    Organization,
    OrganizationMember,
    OrganizationPreferences,
    TenantAccessPolicy,
)
from app.routers import organization_branding as routes
from app.services.branding import provider_link, store
from tests._sqlite_session import org_tables, sqlite_session, with_json_tables

SELLER, BUYER = 5, 9


def _user(group: str, email: str) -> AuthenticatedUser:
    return AuthenticatedUser(user_id=f"{group}:{email}", email=email, groups=[group])


TENANT = _user("role_tenant", "tenant@buyer.test")
SELLER_OPS = _user("role_partner", "ops@seller.test")
ADMIN = _user("role_admin", "admin@op.test")


@pytest.fixture(autouse=True)
def quiet_audit(monkeypatch):
    class _Logger:
        def log_activity(self, **_kwargs):
            pass

    monkeypatch.setattr(store, "get_activity_logger", lambda: _Logger())
    monkeypatch.setattr(provider_link, "get_activity_logger", lambda: _Logger())


@pytest.fixture
async def db():
    db = with_json_tables(
        sqlite_session(*org_tables()), OrganizationPreferences, DeploymentBranding, TenantAccessPolicy,
    )
    db.add_all([
        Organization(id=SELLER, name="Seller Cloud", slug="seller-cloud", created_by="ops@seller.test", source="local"),
        Organization(id=BUYER, name="Buyer", slug="buyer", created_by="ops@seller.test", source="orcahub",
                     provider_organization_id=SELLER, provider_link_source="provision"),
    ])
    await db.commit()
    db.add_all([
        OrganizationMember(organization_id=SELLER, user_email="ops@seller.test", role="member"),
        OrganizationMember(organization_id=BUYER, user_email="tenant@buyer.test", role="tenant"),
        OrganizationPreferences(organization_id=SELLER, settings={"branding": {"appName": "Seller Cloud"}},
                                updated_by="ops@seller.test"),
    ])
    await db.commit()
    return db


async def test_a_tenant_reads_the_inherited_look_without_the_editor_or_the_seller(db) -> None:
    mine = await routes.get_my_branding(Response(), user=TENANT, db=db)
    assert (mine.organization_id, mine.source, mine.updated_by) == (BUYER, "provider", None)
    assert mine.settings["branding"]["appName"] == "Seller Cloud"

    org = await routes.get_org_branding(BUYER, Response(), user=TENANT, db=db)
    assert org.settings == {}
    assert org.effective["branding"]["appName"] == "Seller Cloud"
    assert (org.provider, org.followers_count, org.updated_by) == (None, 0, None)


async def test_the_version_answer_matches_the_full_read(db) -> None:
    mine = await routes.get_my_branding(Response(), user=TENANT, db=db)
    version = await routes.get_my_branding_version(Response(), user=TENANT, db=db)
    assert (version.organization_id, version.version) == (mine.organization_id, mine.version)


async def test_the_seller_sees_who_follows_it_and_where_the_buyer_inherits_from(db) -> None:
    own = await routes.get_org_branding(SELLER, Response(), user=SELLER_OPS, db=db)
    assert (own.followers_count, own.inherits_from, own.updated_by) == (1, "default", "ops@seller.test")

    buyer = await routes.get_org_branding(BUYER, Response(), user=SELLER_OPS, db=db)
    assert (buyer.inherits_from, buyer.provider.id, buyer.provider.name) == ("provider", SELLER, "Seller Cloud")


async def test_an_invalid_write_is_refused_with_its_field(db) -> None:
    with pytest.raises(HTTPException) as refused:
        await routes.update_org_branding(SELLER, routes.OrgBrandingBody(settings={"colors": {"accentColor": "red"}}),
                                         Response(), user=SELLER_OPS, db=db)
    assert refused.value.status_code == 422
    assert "colors.accentColor" in refused.value.detail


async def test_a_partner_cannot_write_an_organization_it_does_not_manage(db) -> None:
    stranger = _user("role_partner", "ops@other.test")
    with pytest.raises(HTTPException) as refused:
        await routes.update_org_branding(SELLER, routes.OrgBrandingBody(settings={"theme": "dark"}),
                                         Response(), user=stranger, db=db)
    assert refused.value.status_code == 403


async def test_an_administrator_sets_and_clears_the_provider(db) -> None:
    cleared = await routes.set_org_provider(BUYER, routes.ProviderBody(provider_organization_id=None), user=ADMIN, db=db)
    assert (cleared.provider, cleared.inherits_from) == (None, "default")
    relinked = await routes.set_org_provider(BUYER, routes.ProviderBody(provider_organization_id=SELLER),
                                             user=ADMIN, db=db)
    assert relinked.provider.link_source == "admin"


def test_my_preferences_is_mounted_before_the_organization_id_route() -> None:
    """Starlette matches in order, and `/{org_id}` would take `my-preferences` and answer 422."""
    main = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    assert main.index("include_router(organization_branding_router") < main.index("include_router(organizations_router")
