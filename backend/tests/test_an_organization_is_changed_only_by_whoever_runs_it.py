"""An organization is renamed, rebranded, bound to or unbound from only by whoever runs it.

PATCH /organizations/{id}, the branding writes, and binding or unbinding a cluster ask the roster
rule (services.organization_roster.user_runs_org): an admin, the creator, or a partner row. Seeing
the organization is not enough, since any member row gives that: a partner who only bought from
another partner, or a dashboard-added teammate, could otherwise rename that partner's organization,
rewrite its branding and unbind its clusters. The rows are real on SQLite; the writes themselves
are recorders.
"""

from contextlib import asynccontextmanager
from typing import Any, List

import pytest
from fastapi import HTTPException, Response

from app.core.auth import AuthenticatedUser
from app.routers import organization_branding, organizations
from app.services.organization_service import OrganizationService
from tests.test_whole_cluster_state_needs_the_whole_cluster import ADMIN, OTHER, SELLER, SOLO, _world

SOLO_ORG = 4  # created by OTHER; SELLER holds only a member row there (a purchase)
SELLERS_BUYER = 2  # created by SELLER


def _user(email: str, group: str = "role_partner") -> AuthenticatedUser:
    return AuthenticatedUser(user_id=f"{group}:{email}", email=email, groups=[group])


@pytest.fixture
def writes(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    done: List[str] = []

    async def _update(self: Any, org_id: int, **_k: Any) -> Any:
        done.append(f"rename {org_id}")
        return None

    async def _unbind(self: Any, org_id: int, cluster_id: str) -> bool:
        done.append(f"unbind {org_id}/{cluster_id}")
        return True

    async def _reset(_db: Any, org_id: int) -> None:
        done.append(f"reset-branding {org_id}")

    @asynccontextmanager
    async def _lock(_db: Any, _key: Any):
        yield

    monkeypatch.setattr(OrganizationService, "update_organization", _update)
    monkeypatch.setattr(OrganizationService, "unbind_cluster", _unbind)
    monkeypatch.setattr(organizations, "_guard_cluster_write", _lock)
    monkeypatch.setattr(organization_branding.store, "reset_org_branding", _reset)
    return done


def _doors(org_id: int, user: AuthenticatedUser, db: Any) -> dict:
    return {
        "rename": lambda: organizations.update_organization(org_id, organizations.OrgUpdateRequest(name="x"), user, db),
        "unbind": lambda: organizations.unbind_cluster(org_id, SOLO, user, db),
        "reset-branding": lambda: organization_branding.reset_org_branding(org_id, user, db, None),
    }


@pytest.mark.parametrize("door", ["rename", "unbind", "reset-branding"])
async def test_a_member_row_changes_nothing_in_an_organization_it_does_not_run(writes, door: str) -> None:
    db = await _world()
    with pytest.raises(HTTPException) as caught:
        await _doors(SOLO_ORG, _user(SELLER), db)[door]()
    assert caught.value.status_code == 403 and writes == []


@pytest.mark.parametrize("door", ["rename", "unbind", "reset-branding"])
async def test_the_creator_and_an_admin_still_change_it(writes, door: str) -> None:
    db = await _world()
    for user in (_user(OTHER), _user(ADMIN, "role_admin")):
        try:
            await _doors(SOLO_ORG, user, db)[door]()
        except HTTPException as exc:
            assert exc.status_code != 403, exc.detail
    assert len(writes) == 2


async def test_reading_the_branding_still_needs_only_to_see_the_organization(monkeypatch: pytest.MonkeyPatch) -> None:
    db = await _world()
    org = await organization_branding._require_org_access(db, SOLO_ORG, _user(SELLER))
    assert org.id == SOLO_ORG
    with pytest.raises(HTTPException):
        await organization_branding._require_org_access(db, SOLO_ORG, _user(SELLER), runs=True)
