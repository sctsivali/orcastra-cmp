"""An organization's roster is changed only by whoever runs it, and an OrcaHub roster keeps its partners.

"May this user see the organization" is true for a tenant row, so it cannot gate the member
routes: a partner who only bought from another partner could change that partner's members. A
co-partner in an OrcaHub-synced organization stays, since the next sync would restore them; only an
Admin removes one. These run the real handlers against SQLite rows.
"""

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, List

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.core.auth import AuthenticatedUser
from app.models.db_models import Organization, OrganizationMember
from app.routers import organizations, user_groups
from app.services import orcahub_sync, organization_roster
from app.services.organization_service import OrganizationService
from tests._sqlite_session import org_tables, sqlite_session

RUNNER = "runner@example.test"
BUYER = "buyer-partner@example.test"
CO = "copartner@example.test"
T = "tenant@example.test"
DASH = "dashboard-added@example.test"
ADMIN = "ops@example.test"


def _user(email: str, group: str = "role_partner") -> AuthenticatedUser:
    return AuthenticatedUser(user_id=f"{group}:{email}", email=email, groups=[group])


async def _db():
    db = sqlite_session(*org_tables())
    db.add_all([
        Organization(id=1, name="Local", slug="local", source="local", created_by="founder@example.test",
                     is_active=True),
        Organization(id=2, name="Synced", slug="synced", source="orcahub", external_id="e2",
                     api_key_id="oak_test", created_by="system", is_active=True),
        Organization(id=3, name="Synced Unkeyed", slug="synced-unkeyed", source="orcahub", external_id="e3",
                     api_key_id=None, created_by="system", is_active=True),
    ])
    await db.commit()
    db.add_all([
        OrganizationMember(organization_id=1, user_email=RUNNER, role="partner"),
        OrganizationMember(organization_id=1, user_email=BUYER, role="tenant"),
        OrganizationMember(organization_id=1, user_email=T, role="tenant"),
        OrganizationMember(organization_id=1, user_email=DASH, role="member"),
        OrganizationMember(organization_id=1, user_email=CO, role="tenant"),
        OrganizationMember(organization_id=2, user_email=RUNNER, role="partner"),
        OrganizationMember(organization_id=2, user_email=CO, role="partner"),
        OrganizationMember(organization_id=2, user_email=T, role="member"),
        OrganizationMember(organization_id=3, user_email=RUNNER, role="partner"),
        OrganizationMember(organization_id=3, user_email=CO, role="partner"),
    ])
    await db.commit()
    return db


async def _members(db: Any, org_id: int) -> List[str]:
    rows = await db.execute(select(OrganizationMember.user_email).where(OrganizationMember.organization_id == org_id))
    return sorted(rows.scalars().all())


@pytest.fixture(autouse=True)
def fenced(monkeypatch: pytest.MonkeyPatch) -> List[int]:
    """The organization fence, recorded: every roster write still runs inside it."""
    entered: List[int] = []

    @asynccontextmanager
    async def _fence(_db: Any, *, organization_id: int) -> AsyncIterator[None]:
        entered.append(organization_id)
        yield

    monkeypatch.setattr(organizations, "guard_organization_mutation", _fence)
    return entered


@pytest.fixture
def pushes(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    sent: List[str] = []

    async def _push(_db: Any, email: str, _org_ids: List[int]) -> bool:
        sent.append(email)
        return True

    class _Authentik:
        async def remove_user_from_all_roles(self, email: str) -> bool:
            sent.append(f"authentik:{email}")
            return True

    monkeypatch.setattr(orcahub_sync, "push_member_removal_to_orcahub", _push)
    monkeypatch.setattr(user_groups, "get_authentik_sync", lambda: _Authentik())
    return sent


# ── the member routes ──────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("email", [BUYER, DASH], ids=["tenant-row", "legacy-member-row"])
async def test_a_member_who_does_not_run_the_org_cannot_change_its_roster(email: str, fenced: List[int]) -> None:
    db = await _db()
    caller = _user(email)
    for call in (
        lambda: organizations.add_member(1, organizations.MemberAddRequest(user_email="new@example.test"), user=caller, db=db),
        lambda: organizations.update_member(1, T, organizations.MemberUpdateRequest(), user=caller, db=db),
        lambda: organizations.remove_member(1, T, user=caller, db=db),
    ):
        with pytest.raises(HTTPException) as caught:
            await call()
        assert caught.value.status_code == 403
    assert await _members(db, 1) == sorted([RUNNER, BUYER, T, DASH, CO])
    assert fenced == [1, 1, 1], "the check still runs inside the organization fence"


async def test_whoever_runs_the_org_changes_its_roster() -> None:
    db = await _db()
    await organizations.add_member(1, organizations.MemberAddRequest(user_email="new@example.test"), user=_user(RUNNER), db=db)
    await organizations.remove_member(1, T, user=_user(RUNNER), db=db)
    assert "new@example.test" in await _members(db, 1) and T not in await _members(db, 1)


async def test_a_creator_whose_own_row_is_member_still_runs_the_org() -> None:
    """create_organization writes the creator's own row as "member", so created_by is what runs it."""
    db = sqlite_session(*org_tables())
    org = await OrganizationService(db).create_organization("Made Here", None, "maker@example.test")
    rows = (await db.execute(select(OrganizationMember.role).where(OrganizationMember.organization_id == org.id))).scalars().all()
    assert rows == ["member"], "precondition: the creator row is the legacy member role"
    maker = _user("maker@example.test")
    await organizations.add_member(org.id, organizations.MemberAddRequest(user_email=T), user=maker, db=db)
    assert T in await _members(db, org.id)
    await organizations.remove_member(org.id, T, user=maker, db=db)
    assert T not in await _members(db, org.id)


async def test_an_admin_runs_every_org_without_a_row() -> None:
    db = await _db()
    assert await organization_roster.user_runs_org(db, 1, ADMIN, "admin") is True
    assert await organization_roster.user_runs_org(db, 99, RUNNER, "partner") is False


@pytest.mark.parametrize("route", ["remove", "update"])
async def test_a_partner_cannot_change_a_co_partner_in_an_orcahub_roster(route: str) -> None:
    db = await _db()
    runner = _user(RUNNER)
    with pytest.raises(HTTPException) as caught:
        if route == "remove":
            await organizations.remove_member(2, CO, user=runner, db=db)
        else:
            await organizations.update_member(2, CO, organizations.MemberUpdateRequest(), user=runner, db=db)
    assert caught.value.status_code == 409
    assert "synced" in caught.value.detail and "Orcastra admin" in caught.value.detail
    assert await _members(db, 2) == sorted([RUNNER, CO, T])
    rows = await db.execute(select(OrganizationMember.role).where(
        OrganizationMember.organization_id == 2, OrganizationMember.user_email == CO))
    assert rows.scalars().all() == ["partner"], "an update must not demote the row either"


async def test_a_legacy_member_row_in_an_orcahub_roster_is_not_a_partner() -> None:
    db = await _db()
    await organizations.remove_member(2, T, user=_user(RUNNER), db=db)
    assert T not in await _members(db, 2)


async def test_an_unkeyed_orcahub_org_is_not_owned_by_orcahub() -> None:
    db = await _db()
    await organizations.remove_member(3, CO, user=_user(RUNNER), db=db)
    assert CO not in await _members(db, 3)


async def test_an_admin_still_changes_an_orcahub_roster() -> None:
    db = await _db()
    await organizations.remove_member(2, CO, user=_user(ADMIN, "role_admin"), db=db)
    assert CO not in await _members(db, 2)


# ── the user list removal ──────────────────────────────────────────────────────────────────────

async def test_a_removal_keeps_only_the_orcahub_partner_rows_and_names_them(pushes: List[str]) -> None:
    db = await _db()
    answer = await user_groups.remove_user_from_all(CO, user=_user(RUNNER), db=db)
    assert answer["kept"] == ["synced"]
    assert "synced" in answer["detail"] and "Orcastra admin" in answer["detail"]
    assert CO not in await _members(db, 1) and CO not in await _members(db, 3)
    assert CO in await _members(db, 2)
    assert pushes == [CO]


async def test_a_removal_that_only_meets_orcahub_partner_rows_is_refused(pushes: List[str]) -> None:
    db = await _db()
    await db.execute(OrganizationMember.__table__.delete().where(
        OrganizationMember.user_email == CO, OrganizationMember.organization_id.in_([1, 3])))
    await db.commit()
    with pytest.raises(HTTPException) as caught:
        await user_groups.remove_user_from_all(CO, user=_user(RUNNER), db=db)
    assert caught.value.status_code == 409 and "synced" in caught.value.detail
    assert CO in await _members(db, 2) and pushes == []


async def test_a_removal_that_reaches_no_org_the_caller_runs_says_so(pushes: List[str]) -> None:
    """A name typed by hand that the caller runs no organization for: the list never shows it."""
    db = await _db()
    with pytest.raises(HTTPException) as caught:
        await user_groups.remove_user_from_all(RUNNER, user=_user(BUYER), db=db)
    assert caught.value.status_code == 404 and "organization you run" in caught.value.detail
    assert RUNNER in await _members(db, 1) and pushes == []


async def test_a_legacy_member_row_in_a_synced_org_is_removed(pushes: List[str]) -> None:
    db = await _db()
    answer = await user_groups.remove_user_from_all(T, user=_user(RUNNER), db=db)
    assert answer["kept"] == []
    assert T not in await _members(db, 1) and T not in await _members(db, 2)


async def test_an_admin_removal_reaches_every_org(pushes: List[str]) -> None:
    db = await _db()
    answer = await user_groups.remove_user_from_all(CO, user=_user(ADMIN, "role_admin"), db=db)
    assert answer["kept"] == []
    assert [CO in await _members(db, org) for org in (1, 2, 3)] == [False, False, False]
    assert pushes == [f"authentik:{CO}", CO]
