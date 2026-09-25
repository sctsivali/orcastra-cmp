"""A member push writes only into an organization the calling key has claimed.

The member path read `if org.api_key_id and ...`, so an organization no key owned was an open
door: any org.sync key could add itself to it as a partner member, or delete its members, without
ever claiming it. The policy sync was already strict for an unclaimed organization. The member
path now is too.

A normal first sync is unaffected, because sync_data runs the organization upsert first and that
claims an unclaimed OrcaHub organization before a member is written. That order also means an
unclaimed organization is claimed and then pruned by the same sync when the reconcile is on. The
first key to push an organization owns it by design, and the test below pins that order so a
change to it is a deliberate one.
"""

from typing import Any, Dict, List, Optional

import pytest
from sqlalchemy import select

from app.models.db_models import Organization, OrganizationMember
from app.routers import integrations
from app.routers.integrations import SyncDataRequest, SyncMember, SyncOrganization
from app.services import authentik_sync
from app.services.integration_roles import ROLE_TENANT
from tests._sqlite_session import AsyncSessionFacade, org_tables, sqlite_session
from tests.test_an_integration_key_never_demotes import FakeAuthentik

KEY = "oak_caller"
OTHER = "oak_other"


@pytest.fixture
def authentik(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    holder: Dict[str, Any] = {"svc": FakeAuthentik({ROLE_TENANT})}
    monkeypatch.setattr(authentik_sync, "get_authentik_sync", lambda: holder["svc"])
    return holder


def _settings(monkeypatch: pytest.MonkeyPatch, *, reconcile: bool) -> None:
    real = integrations.get_settings()
    patched = real.model_copy(update={
        "member_reconcile_enabled": reconcile, "member_reconcile_dry_run": False,
    })
    monkeypatch.setattr(integrations, "get_settings", lambda: patched)


async def _legacy_org(owner: Optional[str], members: Dict[str, str]) -> AsyncSessionFacade:
    db = sqlite_session(*org_tables())
    db.add(Organization(
        name="Legacy", slug="legacy", source="orcahub", external_id="ext-legacy",
        api_key_id=owner, created_by="system", is_active=True,
    ))
    await db.commit()
    for email, role in members.items():
        db.add(OrganizationMember(organization_id=1, user_email=email, role=role))
    await db.commit()
    return db


async def _members(db: AsyncSessionFacade, org_id: int = 1) -> List[str]:
    return sorted((await db.execute(
        select(OrganizationMember.user_email).where(OrganizationMember.organization_id == org_id)
    )).scalars().all())


async def _owner(db: AsyncSessionFacade, org_id: int = 1) -> Optional[str]:
    return (await db.execute(
        select(Organization.api_key_id).where(Organization.id == org_id)
    )).scalar_one()


def _member(email: str, role: str = "TENANT", org: str = "ext-legacy", action: str = "upsert") -> SyncMember:
    return SyncMember(email=email, organization_id=org, role=role, action=action)


def _key() -> Dict[str, Any]:
    return {"id": KEY, "capabilities": ["inventory.read", "org.sync"], "cluster_permissions": []}


async def _sync(
    db: AsyncSessionFacade, members: List[SyncMember], orgs: Optional[List[SyncOrganization]] = None
) -> Any:
    request = SyncDataRequest(
        include_clusters=False, include_projects=False, include_instances=False,
        organizations=orgs or [], members=members,
    )
    return await integrations.sync_data(request, _key(), None, db)


LEGACY = SyncOrganization(id="ext-legacy", name="Legacy", slug="legacy")


# ── the member path refuses an org this key did not claim ───────────────────────────────────────

@pytest.mark.parametrize("owner", [None, OTHER])
async def test_a_member_push_into_an_org_this_key_did_not_claim_writes_nothing(
    authentik: Dict[str, Any], owner: Optional[str]
) -> None:
    db = await _legacy_org(owner, {"victim@example.test": "partner"})
    synced, removed = await integrations._upsert_synced_members(db, [
        _member("attacker@example.test", "PARTNER"),
        _member("victim@example.test", "PARTNER", action="remove"),
    ], KEY)
    assert (synced, removed) == (0, 0)
    assert await _members(db) == ["victim@example.test"]
    assert await _owner(db) == owner
    assert authentik["svc"].changes == []


async def test_a_members_only_sync_for_a_still_unclaimed_org_is_refused(authentik: Dict[str, Any]) -> None:
    db = await _legacy_org(None, {"victim@example.test": "tenant"})
    result = await _sync(db, [_member("attacker@example.test", "PARTNER")])
    assert result.members_synced == 0
    assert await _members(db) == ["victim@example.test"]
    assert await _owner(db) is None
    assert authentik["svc"].changes == []


async def test_the_claiming_key_writes_its_members(authentik: Dict[str, Any]) -> None:
    db = await _legacy_org(KEY, {})
    synced, _ = await integrations._upsert_synced_members(db, [_member("new@example.test")], KEY)
    assert synced == 1
    assert await _members(db) == ["new@example.test"]


# ── a normal first sync still works end to end ──────────────────────────────────────────────────

async def test_a_first_sync_claims_an_unclaimed_org_and_writes_its_members(
    authentik: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings(monkeypatch, reconcile=False)
    db = await _legacy_org(None, {})
    result = await _sync(db, [_member("first@example.test")], orgs=[LEGACY])
    assert (result.organizations_synced, result.members_synced) == (1, 1)
    assert await _owner(db) == KEY
    assert await _members(db) == ["first@example.test"]


async def test_a_first_sync_of_a_new_org_writes_its_members(
    authentik: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings(monkeypatch, reconcile=False)
    db = sqlite_session(*org_tables())
    fresh = SyncOrganization(id="ext-new", name="New", slug="new")
    result = await _sync(db, [_member("first@example.test", org="ext-new")], orgs=[fresh])
    assert (result.organizations_synced, result.members_synced) == (1, 1)
    assert await _owner(db) == KEY
    assert await _members(db) == ["first@example.test"]


async def test_a_first_sync_does_not_claim_another_keys_org(
    authentik: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings(monkeypatch, reconcile=False)
    db = await _legacy_org(OTHER, {"theirs@example.test": "tenant"})
    result = await _sync(db, [_member("mine@example.test")], orgs=[LEGACY])
    assert (result.organizations_synced, result.members_synced) == (0, 0)
    assert await _owner(db) == OTHER
    assert await _members(db) == ["theirs@example.test"]


# ── the order: claim, members, then the reconcile ───────────────────────────────────────────────

async def test_an_unclaimed_org_is_claimed_then_pruned_by_the_same_sync(
    authentik: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pinned on purpose: the first key to push an unclaimed org owns it, roster included."""
    _settings(monkeypatch, reconcile=True)
    db = await _legacy_org(None, {"old1@example.test": "tenant", "old2@example.test": "tenant"})
    result = await _sync(db, [_member("keep@example.test")], orgs=[LEGACY])
    assert await _owner(db) == KEY
    assert result.members_synced == 1
    assert result.members_removed == 2
    assert await _members(db) == ["keep@example.test"]
