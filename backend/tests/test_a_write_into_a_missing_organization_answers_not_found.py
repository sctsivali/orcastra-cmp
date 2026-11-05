"""Adding a member or binding a cluster to an organization that does not exist answers 404.

An Admin runs every organization (user_runs_org answers True without reading the row), so nothing
stopped a write into one that had been deleted: the insert hit the organization foreign key and the
caller got a bare 500. Found on the RC5 acceptance run, when the migration lab setup reused the id
of the lab organization the previous round had torn down (2026-11-05, org 493).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import pytest
from fastapi import HTTPException

from app.routers import organizations
from app.routers.organizations import ClusterBindRequest, MemberAddRequest

MISSING = 493


class _Service:
    writes: list[str] = []

    def __init__(self, _db: Any) -> None:
        pass

    async def get_organization(self, org_id: int) -> object | None:
        return None

    async def add_member(self, **_kw: Any) -> object:
        _Service.writes.append("add_member")
        raise AssertionError("the insert must not run for a missing organization")

    async def bind_cluster(self, *_a: Any, **_kw: Any) -> object:
        _Service.writes.append("bind_cluster")
        raise AssertionError("the insert must not run for a missing organization")


@pytest.fixture(autouse=True)
def admin_on_a_missing_org(monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def guard(_db: Any, _org_id: int) -> AsyncIterator[None]:
        yield

    async def runs(*_a: Any) -> bool:
        return True

    async def owned(*_a: Any) -> set[str]:
        return set()

    _Service.writes = []
    monkeypatch.setattr(organizations, "_guard_organization_write", guard)
    monkeypatch.setattr(organizations, "OrganizationService", _Service)
    monkeypatch.setattr(organizations, "_get_user_role", lambda _user: "admin")
    monkeypatch.setattr(organizations, "user_runs_org", runs)
    monkeypatch.setattr(organizations, "get_owned_clusters_ignoring_admin", owned)


class _User:
    email = "qa-admin@example.test"


async def test_adding_a_member_to_a_missing_organization_is_a_404() -> None:
    with pytest.raises(HTTPException) as caught:
        await organizations.add_member(
            MISSING, MemberAddRequest(user_email="someone@example.test"), user=_User(), db=object()
        )

    assert caught.value.status_code == 404
    assert _Service.writes == []


async def test_binding_a_cluster_to_a_missing_organization_is_a_404() -> None:
    with pytest.raises(HTTPException) as caught:
        await organizations.bind_cluster(
            MISSING, ClusterBindRequest(cluster_id="mig-lab-a"), user=_User(), db=object()
        )

    assert caught.value.status_code == 404
    assert _Service.writes == []
