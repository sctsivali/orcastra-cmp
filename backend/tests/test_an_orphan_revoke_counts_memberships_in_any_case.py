"""An orphan revoke counts the user's remaining memberships whatever the email's case.

Membership rows are unique per (organization, user_email) with the email compared exactly, so one
address can sit in two organizations under two spellings. revoke_if_orphaned strips role_tenant
once the user belongs to no organization. An exact-case count would read a user still in another
organization under another spelling as an orphan and strip the role they still use, so the count
folds case (services/integration_roles.revoke_if_orphaned).
"""

from typing import Any, Dict

import pytest

from app.models.db_models import Organization, OrganizationMember
from app.routers import integrations
from app.routers.integrations import SyncMember
from app.services import authentik_sync
from app.services.integration_roles import ROLE_TENANT, revoke_if_orphaned
from tests._sqlite_session import AsyncSessionFacade, org_tables, sqlite_session
from tests.test_an_integration_key_never_demotes import FakeAuthentik

KEY = "oak_test"


async def _two_orgs() -> AsyncSessionFacade:
    """The user is in the key's org as bob@, and in a dashboard org as Bob@."""
    db = sqlite_session(*org_tables())
    db.add(Organization(
        name="Synced", slug="synced", source="orcahub", external_id="ext-x",
        api_key_id=KEY, created_by="system", is_active=True,
    ))
    db.add(Organization(name="Local", slug="local", source="local", created_by="owner@example.test"))
    await db.commit()
    db.add(OrganizationMember(organization_id=1, user_email="bob@example.test", role="tenant"))
    db.add(OrganizationMember(organization_id=2, user_email="Bob@Example.TEST", role="tenant"))
    await db.commit()
    return db


@pytest.fixture
def authentik(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    holder: Dict[str, Any] = {"svc": FakeAuthentik({ROLE_TENANT})}
    monkeypatch.setattr(authentik_sync, "get_authentik_sync", lambda: holder["svc"])
    return holder


async def test_leaving_one_org_keeps_role_tenant_held_through_another_spelling(
    authentik: Dict[str, Any],
) -> None:
    db = await _two_orgs()
    _, removed = await integrations._upsert_synced_members(
        db, [SyncMember(email="bob@example.test", organization_id="ext-x", role="TENANT", action="remove")], KEY
    )
    assert removed == 1
    assert authentik["svc"].changes == []
    assert authentik["svc"].groups == {ROLE_TENANT}


@pytest.mark.parametrize("spelling", ["bob@example.test", "BOB@EXAMPLE.TEST", "Bob@Example.TEST"])
async def test_the_count_matches_any_spelling(authentik: Dict[str, Any], spelling: str) -> None:
    db = await _two_orgs()
    change = await revoke_if_orphaned(db, authentik["svc"], spelling)
    assert change.is_noop and change.reason == "still_a_member"
    assert authentik["svc"].changes == []


async def test_a_user_with_no_membership_left_in_any_spelling_loses_role_tenant(
    authentik: Dict[str, Any],
) -> None:
    """The positive twin: without it the tests above pass against a revoke that never runs."""
    db = sqlite_session(*org_tables())
    change = await revoke_if_orphaned(db, authentik["svc"], "Bob@Example.TEST")
    assert change.reason == "revoke_tenant"
    assert authentik["svc"].groups == set()
