"""An organization's roster is changed only by whoever runs it, and an OrcaHub roster keeps its partners.

The member routes checked "may this user see the organization", which is true for a tenant row,
so a partner who only bought from another partner could add, change and remove that partner's
members. A partner could also remove a co-partner from an OrcaHub-synced organization, which the
next sync quietly undid. These run the real handlers against SQLite rows.
"""

from pathlib import Path
from typing import Any, List

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.core.auth import AuthenticatedUser
from app.models.db_models import Organization, OrganizationMember
from app.routers import organizations, user_groups
from app.services import orcahub_sync
from tests._sqlite_session import org_tables, sqlite_session

RUNNER = "runner@example.test"
BUYER = "buyer-partner@example.test"
CO = "copartner@example.test"
T = "tenant@example.test"
DASH = "dashboard-added@example.test"


def _user(email: str, group: str = "role_partner") -> AuthenticatedUser:
    return AuthenticatedUser(user_id=f"{group}:{email}", email=email, groups=[group])


async def _db():
    db = sqlite_session(*org_tables())
    db.add_all([
        Organization(id=1, name="Local", slug="local", source="local", created_by="founder@example.test"),
        Organization(id=2, name="Synced", slug="synced", source="orcahub", external_id="e2",
                     api_key_id="oak_test", created_by="system"),
    ])
    await db.commit()
    db.add_all([
        OrganizationMember(organization_id=1, user_email=RUNNER, role="partner"),
        OrganizationMember(organization_id=1, user_email=BUYER, role="tenant"),
        OrganizationMember(organization_id=1, user_email=T, role="tenant"),
        OrganizationMember(organization_id=1, user_email=DASH, role="member"),
        OrganizationMember(organization_id=2, user_email=RUNNER, role="partner"),
        OrganizationMember(organization_id=2, user_email=CO, role="partner"),
        OrganizationMember(organization_id=2, user_email=T, role="member"),
    ])
    await db.commit()
    return db


async def _members(db, org_id: int) -> List[str]:
    rows = await db.execute(select(OrganizationMember.user_email).where(OrganizationMember.organization_id == org_id))
    return sorted(rows.scalars().all())


@pytest.mark.parametrize("email", [BUYER, DASH], ids=["tenant-row", "legacy-member-row"])
async def test_a_tenant_member_cannot_change_the_roster(email: str) -> None:
    """A partner account whose row here is tenant, or the legacy "member" the add route writes."""
    db = await _db()
    buyer = _user(email)
    for call in (
        lambda: organizations.add_member(1, organizations.MemberAddRequest(user_email="new@example.test"), user=buyer, db=db),
        lambda: organizations.update_member(1, T, organizations.MemberUpdateRequest(role="member"), user=buyer, db=db),
        lambda: organizations.remove_member(1, T, user=buyer, db=db),
    ):
        with pytest.raises(HTTPException) as caught:
            await call()
        assert caught.value.status_code == 403
    assert await _members(db, 1) == sorted([RUNNER, BUYER, T, DASH])


async def test_whoever_runs_the_org_changes_its_roster() -> None:
    db = await _db()
    await organizations.remove_member(1, T, user=_user(RUNNER), db=db)
    assert T not in await _members(db, 1)


@pytest.mark.parametrize("route", ["remove", "update"])
async def test_a_partner_cannot_change_a_co_partner_in_an_orcahub_roster(route: str) -> None:
    db = await _db()
    runner = _user(RUNNER)
    with pytest.raises(HTTPException) as caught:
        if route == "remove":
            await organizations.remove_member(2, CO, user=runner, db=db)
        else:
            await organizations.update_member(2, CO, organizations.MemberUpdateRequest(role="member"), user=runner, db=db)
    assert caught.value.status_code == 409
    assert "OrcaHub" in caught.value.detail and "Orcastra admin" in caught.value.detail
    assert CO in await _members(db, 2)


async def test_an_admin_still_changes_an_orcahub_roster() -> None:
    db = await _db()
    await organizations.remove_member(2, CO, user=_user("admin@example.test", "role_admin"), db=db)
    assert CO not in await _members(db, 2)


async def test_a_legacy_member_row_is_not_mistaken_for_a_partner(monkeypatch) -> None:
    db = await _db()
    pushes: List[Any] = []

    async def spy(_db: Any, email: str, org_ids: Any) -> bool:
        pushes.append(set(org_ids))
        return True

    class _NoAuthentik:
        async def remove_user_from_all_roles(self, *_a: Any) -> bool:
            return True

    monkeypatch.setattr(orcahub_sync, "push_member_removal_to_orcahub", spy)
    monkeypatch.setattr(user_groups, "get_authentik_sync", lambda: _NoAuthentik())
    await user_groups.remove_user_from_all(T, user=_user(RUNNER), db=db)
    assert T not in await _members(db, 2)
    assert pushes and 2 in pushes[0], "a legacy member row in a synced org is removed there too, so OrcaHub is told"


def test_the_remove_dialog_shows_the_refusal() -> None:
    source = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "user-groups-settings.tsx").read_text()
    dialog = source[source.index("<DialogTitle>Remove User</DialogTitle>") :]
    dialog = dialog[: dialog.index("</DialogContent>")]
    assert "{deleteMutation.error && (" in dialog, "a refused removal must say why"
    assert "(deleteMutation.error as Error).message" in dialog
    assert "deleteMutation.reset()" in source, "one row's refusal must not show on another row"
    partial = source[source.index("if (result.kept?.length)") :]
    partial = partial[: partial.index("}") + 1]
    assert "toast(" in partial and "description: result.detail" in partial, (
        "a partial removal must say which organizations kept the user"
    )
