"""The User Groups routes reach only the organizations their caller runs.

Run against the real handlers and SQLite rows:

- A removal tells OrcaHub only about the keyed organizations it removed the row from, so a partner
  removing a tenant never removes that person from another partner's organization on the next
  sync, nor from a synced organization whose partner row was kept.
- A partner's list leaves out the roster of an organization it only buys from.
- Emails match case-insensitively, as Authentik matches them, so one account is listed once and a
  removal finds its row.
"""

from typing import Any, Dict, List, Tuple

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.core.auth import AuthenticatedUser
from app.models.db_models import Organization, OrganizationMember
from app.routers import user_groups
from app.services import orcahub_sync
from tests._sqlite_session import org_tables, sqlite_session

RUNNER = "runner@example.com"
OTHER = "other-partner@example.com"
T = "tenant@example.com"
CO = "copartner@example.com"
ADMIN = "ops@example.com"


def _user(email: str, group: str = "role_partner") -> AuthenticatedUser:
    return AuthenticatedUser(user_id=f"{group}:{email}", email=email, groups=[group])


async def _db():
    """Org 1 local and 2 synced are RUNNER's; 4 synced is OTHER's, where RUNNER only buys."""
    db = sqlite_session(*org_tables())
    db.add_all([
        Organization(id=1, name="Local", slug="local", source="local", created_by="founder@example.com",
                     is_active=True),
        Organization(id=2, name="Synced", slug="synced", source="orcahub", external_id="e2",
                     api_key_id="oak_runner", created_by="system", is_active=True),
        Organization(id=3, name="Synced Unkeyed", slug="synced-unkeyed", source="orcahub", external_id="e3",
                     api_key_id=None, created_by="system", is_active=True),
        Organization(id=4, name="Other", slug="other", source="orcahub", external_id="e4",
                     api_key_id="oak_other", created_by="system", is_active=True),
    ])
    await db.commit()
    db.add_all([
        OrganizationMember(organization_id=1, user_email=RUNNER, role="partner"),
        OrganizationMember(organization_id=1, user_email=T, role="tenant"),
        OrganizationMember(organization_id=1, user_email=CO, role="tenant"),
        OrganizationMember(organization_id=2, user_email=RUNNER, role="partner"),
        OrganizationMember(organization_id=2, user_email=T, role="member"),
        OrganizationMember(organization_id=2, user_email=CO, role="partner"),
        OrganizationMember(organization_id=3, user_email=RUNNER, role="partner"),
        OrganizationMember(organization_id=3, user_email=CO, role="partner"),
        OrganizationMember(organization_id=4, user_email=OTHER, role="partner"),
        OrganizationMember(organization_id=4, user_email=RUNNER, role="tenant"),
        OrganizationMember(organization_id=4, user_email=T, role="tenant"),
    ])
    await db.commit()
    return db


async def _orgs_of(db: Any, email: str) -> List[int]:
    rows = await db.execute(
        select(OrganizationMember.organization_id).where(OrganizationMember.user_email == email)
    )
    return sorted(rows.scalars().all())


class _Authentik:
    def __init__(self) -> None:
        self.calls: List[str] = []

    async def remove_user_from_all_roles(self, email: str) -> bool:
        self.calls.append(f"remove:{email}")
        return True

    async def get_user_role_groups(self, email: str) -> set:
        return set()

    async def sync_user_role(self, email: str, role: str) -> bool:
        self.calls.append(f"sync:{email}:{role}")
        return True


@pytest.fixture
def authentik(monkeypatch: pytest.MonkeyPatch) -> _Authentik:
    fake = _Authentik()
    monkeypatch.setattr(user_groups, "get_authentik_sync", lambda: fake)
    return fake


@pytest.fixture
def orcahub(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[int, str, str]]:
    """Every member push the real removal service makes, as (org id, email, action)."""
    sent: List[Tuple[int, str, str]] = []

    async def _push(_db: Any, org_id: int, members: List[Dict[str, Any]]) -> bool:
        sent.extend((org_id, m["email"], m.get("action", "upsert")) for m in members)
        return True

    monkeypatch.setattr(orcahub_sync, "push_members_to_orcahub", _push)
    return sent


# ── removal reaches OrcaHub only for the organizations it removed from ────────────────────────

async def test_a_partner_removal_never_reaches_another_partners_orcahub_org(authentik, orcahub) -> None:
    db = await _db()
    await user_groups.remove_user_from_all(T, user=_user(RUNNER), db=db)
    assert await _orgs_of(db, T) == [4], "the row in OTHER's organization is not the caller's to remove"
    assert orcahub == [(2, T, "remove")], "only the synced organization the row left hears about it"


async def test_a_kept_synced_partner_row_is_not_removed_on_orcahubs_side(authentik, orcahub) -> None:
    db = await _db()
    answer = await user_groups.remove_user_from_all(CO, user=_user(RUNNER), db=db)
    assert answer["kept"] == ["synced"]
    assert await _orgs_of(db, CO) == [2]
    assert orcahub == [], "org 1 is local, org 3 has no key, and org 2 kept its row"


async def test_an_admin_removal_reaches_each_keyed_org_it_removed_from(authentik, orcahub) -> None:
    db = await _db()
    await user_groups.remove_user_from_all(T, user=_user(ADMIN, "role_admin"), db=db)
    assert await _orgs_of(db, T) == []
    assert sorted(orcahub) == [(2, T, "remove"), (4, T, "remove")]
    assert authentik.calls == [f"remove:{T}"]


async def test_two_removed_orgs_on_one_key_are_pushed_once(authentik, orcahub) -> None:
    db = await _db()
    db.add(Organization(id=5, name="Same Key", slug="same-key", source="orcahub", external_id="e5",
                        api_key_id="oak_runner", created_by=RUNNER, is_active=True))
    db.add(OrganizationMember(organization_id=5, user_email=T, role="tenant"))
    await db.commit()
    await user_groups.remove_user_from_all(T, user=_user(RUNNER), db=db)
    assert len(orcahub) == 1 and orcahub[0][0] in (2, 5), "the callback names no org, so one call per key"


# ── the list shows a partner only the organizations it runs ───────────────────────────────────

async def test_a_partner_list_leaves_out_an_org_it_only_buys_from() -> None:
    db = await _db()
    listed = {entry.user_email: entry for entry in await user_groups.list_user_groups(user=_user(RUNNER), db=db)}
    assert OTHER not in listed, "OTHER sits only in org 4, which RUNNER buys from"
    assert sorted(org["id"] for org in listed[T].organizations) == [1, 2]
    assert sorted(org["id"] for org in listed[RUNNER].organizations) == [1, 2, 3]


async def test_a_partner_who_runs_nothing_lists_nobody() -> None:
    db = await _db()
    assert await user_groups.list_user_groups(user=_user("nobody@example.com"), db=db) == []


async def test_an_admin_lists_every_membership_with_each_orgs_role() -> None:
    db = await _db()
    listed = {entry.user_email: entry for entry in await user_groups.list_user_groups(
        user=_user(ADMIN, "role_admin"), db=db)}
    assert OTHER in listed
    roles = {org["id"]: org["role"] for org in listed[RUNNER].organizations}
    assert roles == {1: "partner", 2: "partner", 3: "partner", 4: "tenant"}
    assert listed[RUNNER].role == "partner", "the summary role is still the highest"
    assert {org["id"]: org["role"] for org in listed[T].organizations}[2] == "tenant", "member reads as tenant"


# ── one person, one email, whatever the case ──────────────────────────────────────────────────

async def test_an_assigned_email_matches_its_existing_row_whatever_its_case(authentik, orcahub) -> None:
    db = await _db()
    request = user_groups.AssignRoleRequest(user_email="  Tenant@Example.COM ", role="tenant", organization_id=1)
    await user_groups.assign_user_role(request, user=_user(ADMIN, "role_admin"), db=db)
    rows = (await db.execute(select(OrganizationMember.user_email).where(
        OrganizationMember.organization_id == 1))).scalars().all()
    assert sorted(rows) == sorted([RUNNER, T, CO]), "no second row for the same person"
    assert authentik.calls == ["sync:Tenant@example.com:tenant"]


async def test_a_new_address_keeps_the_case_it_was_typed_with(authentik, orcahub) -> None:
    """The authorization paths compare the token's email exactly, so a row is stored as typed.

    Lowercasing it would lock out an account whose identity provider email has capitals.
    """
    db = await _db()
    for spelled in ("New.Person@Example.com", "new.person@EXAMPLE.com"):
        answer = await user_groups.assign_user_role(
            user_groups.AssignRoleRequest(user_email=spelled, role="tenant", organization_id=1),
            user=_user(ADMIN, "role_admin"), db=db)
        assert answer.organization_id == 1
    rows = (await db.execute(select(OrganizationMember.user_email).where(
        OrganizationMember.organization_id == 1, OrganizationMember.user_email.ilike("new.person%")))).scalars().all()
    assert rows == ["New.Person@example.com"]


@pytest.mark.parametrize("blank", ["", " ", "%20", "no-at-sign", "a b@example.com"])
async def test_a_removal_path_that_is_not_an_address_is_refused(authentik, orcahub, blank: str) -> None:
    db = await _db()
    with pytest.raises(HTTPException) as caught:
        await user_groups.remove_user_from_all(blank, user=_user(ADMIN, "role_admin"), db=db)
    assert caught.value.status_code == 422
    assert authentik.calls == [] and orcahub == []


async def test_a_key_the_person_still_holds_a_row_on_is_not_told_to_remove_them(authentik, orcahub) -> None:
    """The callback names no organization, so OrcaHub reads the key. A key the person still sits
    under, in an organization the removal did not reach, hears nothing."""
    db = await _db()
    db.add(Organization(id=6, name="Seller Other", slug="seller-other", source="orcahub", external_id="e6",
                        api_key_id="oak_runner", created_by="system", is_active=True))
    db.add(OrganizationMember(organization_id=6, user_email=T, role="tenant"))
    await db.commit()
    await user_groups.remove_user_from_all(T, user=_user(RUNNER), db=db)
    assert await _orgs_of(db, T) == [4, 6]
    assert orcahub == [], "oak_runner still carries the person in org 6"


async def test_a_removal_matches_the_row_whatever_case_the_path_carries(authentik, orcahub) -> None:
    db = await _db()
    await user_groups.remove_user_from_all("Tenant@EXAMPLE.com", user=_user(RUNNER), db=db)
    assert await _orgs_of(db, T) == [4]


async def test_a_legacy_mixed_case_row_lists_as_the_same_person() -> None:
    db = await _db()
    db.add(OrganizationMember(organization_id=3, user_email="Tenant@Example.com", role="tenant"))
    await db.commit()
    listed = [e for e in await user_groups.list_user_groups(user=_user(RUNNER), db=db)
              if e.user_email.lower() == T]
    assert len(listed) == 1 and sorted(o["id"] for o in listed[0].organizations) == [1, 2, 3]


async def test_a_removal_answer_is_refused_for_an_org_the_partner_only_buys_from(authentik, orcahub) -> None:
    db = await _db()
    with pytest.raises(HTTPException) as caught:
        await user_groups.remove_user_from_all(OTHER, user=_user(RUNNER), db=db)
    assert caught.value.status_code == 404
    assert await _orgs_of(db, OTHER) == [4] and orcahub == []
