"""With INTEGRATION_MULTI_OWNER_ENABLED off, an organization has one owner: its primary key.

Turning owner sets off is documented as OrcaHub falling back to one owner key. The owner check
ignored the setting, so keys enrolled while it was on kept syncing members and provisioning, while
the routes that remove them answered 404. The check now honours only the primary while the setting
is off, and an administrator can still remove a leftover owner row.
"""

import pytest
from fastapi.routing import APIRoute

from app.models.db_models import Organization, OrganizationOwnerKey
from app.routers import integrations
from app.services import organization_owners
from tests._sqlite_session import org_tables, sqlite_session


async def _org():
    db = sqlite_session(*org_tables())
    db.add_all([
        Organization(id=1, name="o", slug="o", created_by="system", is_active=True, source="orcahub", api_key_id="oak_a"),
        OrganizationOwnerKey(organization_id=1, api_key_id="oak_a", added_via="claim"),
        OrganizationOwnerKey(organization_id=1, api_key_id="oak_b", added_via="enroll"),
    ])
    await db.commit()
    return db, await db.get(Organization, 1)


@pytest.mark.parametrize("enabled,expected", [(True, True), (False, False)])
async def test_a_co_owner_counts_only_while_owner_sets_are_on(monkeypatch, enabled, expected) -> None:
    monkeypatch.setattr(organization_owners, "multi_owner_enabled", lambda: enabled)
    db, org = await _org()
    assert await organization_owners.is_owner(db, org, "oak_a") is True, "the primary owns it either way"
    assert await organization_owners.is_owner(db, org, "oak_b") is expected


def test_an_administrator_can_remove_an_owner_row_with_the_setting_off() -> None:
    route = next(r for r in integrations.router.routes
                 if isinstance(r, APIRoute) and r.endpoint is integrations.remove_organization_owner)
    calls = {d.call for d in route.dependant.dependencies}
    assert integrations._require_multi_owner not in calls


@pytest.mark.parametrize("enabled,expected", [(True, [1]), (False, [])])
async def test_every_ownership_lookup_follows_the_setting(monkeypatch, enabled, expected) -> None:
    """Provisioning, revoke, the policy read and the rebind count look organizations up by
    owned_by_clause, so a leftover co-owner may not provision or read policies either."""
    from sqlalchemy import select

    monkeypatch.setattr(organization_owners, "multi_owner_enabled", lambda: enabled)
    db, _unused = await _org()
    rows = (await db.execute(select(Organization.id).where(organization_owners.owned_by_clause(["oak_b"])))).scalars().all()
    assert list(rows) == expected
    primary = (await db.execute(select(Organization.id).where(organization_owners.owned_by_clause(["oak_a"])))).scalars().all()
    assert list(primary) == [1], "the primary owns it either way"
