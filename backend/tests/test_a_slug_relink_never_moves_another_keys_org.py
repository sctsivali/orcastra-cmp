"""A slug re-link must not move an organization another integration key owns.

Pass 2 of the organization upsert re-links a source='orcahub' row whose external_id is absent
from the push, because OrcaHub slugs are globally unique and such a row is the deleted
predecessor of the incoming org. It did so for any key, so a key could take another
integration's org by pushing its slug under a fresh external_id, then push members into it,
which also pushes roles into Authentik. Pass 1 already refused the same move for a known
identity. These run the real statements against SQLite rows.
"""

from typing import Any, List

import pytest
from sqlalchemy import select

from app.models.db_models import Organization, OrganizationMember
from app.routers import integrations
from app.routers.integrations import SyncMember, SyncOrganization
from tests._sqlite_session import org_tables, sqlite_session


class _NoAuthentik:
    def __init__(self) -> None:
        self.calls: List[tuple] = []

    async def sync_user_role(self, *args: Any) -> bool:
        self.calls.append(("sync_user_role",) + args)
        return True

    async def get_user_role_groups(self, *args: Any) -> None:
        self.calls.append(("get_user_role_groups",) + args)
        return None

    async def remove_user_from_all_roles(self, *args: Any) -> bool:
        self.calls.append(("remove_user_from_all_roles",) + args)
        return True


@pytest.fixture
def authentik(monkeypatch):
    fake = _NoAuthentik()
    import app.services.authentik_sync as authentik_sync

    monkeypatch.setattr(authentik_sync, "get_authentik_sync", lambda: fake)

    async def _no_reconcile(*_a: Any, **_k: Any) -> int:
        return 0

    monkeypatch.setattr(integrations, "reconcile_personal_clusters", _no_reconcile)
    return fake


async def _seed(api_key_id):
    db = sqlite_session(*org_tables())
    db.add(Organization(
        name="Old Name", slug="org-a", source="orcahub", external_id="ext-old",
        api_key_id=api_key_id, created_by="system", is_active=True,
    ))
    await db.commit()
    return db


async def _row(db) -> Organization:
    return (await db.execute(select(Organization).where(Organization.slug == "org-a"))).scalar_one()


async def test_a_foreign_key_cannot_relink_an_owned_org():
    db = await _seed("oak_test_owner")
    synced = await integrations._upsert_synced_organizations(
        db, [SyncOrganization(id="ext-new", name="x", slug="org-a")], "oak_test_other"
    )
    row = await _row(db)
    assert synced == 0
    assert row.api_key_id == "oak_test_owner"
    assert row.external_id == "ext-old"
    assert row.name == "Old Name"


async def test_the_owner_still_relinks_its_retired_identity():
    db = await _seed("oak_test_owner")
    synced = await integrations._upsert_synced_organizations(
        db, [SyncOrganization(id="ext-new", name="New Name", slug="org-a")], "oak_test_owner"
    )
    row = await _row(db)
    assert synced == 1
    assert row.external_id == "ext-new"
    assert row.name == "New Name"
    assert row.api_key_id == "oak_test_owner"


async def test_an_unclaimed_org_can_still_be_relinked():
    """Consistent with Pass 1, which lets a key claim an org no key owns yet."""
    db = await _seed(None)
    synced = await integrations._upsert_synced_organizations(
        db, [SyncOrganization(id="ext-new", name="x", slug="org-a")], "oak_test_other"
    )
    row = await _row(db)
    assert synced == 1
    assert row.external_id == "ext-new"
    assert row.api_key_id == "oak_test_other"


async def test_members_do_not_follow_a_refused_relink(authentik):
    db = await _seed("oak_test_owner")
    await integrations._upsert_synced_organizations(
        db, [SyncOrganization(id="ext-new", name="x", slug="org-a")], "oak_test_other"
    )
    result = await integrations._upsert_synced_members(
        db,
        [SyncMember(email="p2@example.test", organization_id="ext-new", role="PARTNER")],
        "oak_test_other",
    )
    members = (await db.execute(select(OrganizationMember))).scalars().all()
    assert result == (0, 0)
    assert members == []
    assert authentik.calls == [], "no Authentik role change for a refused org"
