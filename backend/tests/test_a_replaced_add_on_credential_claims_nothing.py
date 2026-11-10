"""An add-on credential first-claims an organization only while it is its installation's current key.

A rotation with a grace window leaves the previous credential authenticating until the window ends,
and the carry that moves the add-on's organizations runs once, so an organization the previous
credential claimed afterwards would stay on a key about to expire, with no record. The sync's three
claim arms and provisioning ask is_current_add_on_credential first, under the key's lock, and refuse
a replaced key, one rotated with no grace, or one whose installation was deleted.
"""

import asyncio
import inspect
import re
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, List, Optional

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.db_models import Organization
from app.routers import integrations
from app.routers.integrations import provision_tenant_access
from tests._sqlite_session import installation_rows, org_tables, sqlite_session
from tests.test_duplicate_org_slug_is_a_conflict import _Body, _SlugTakenSession
from tests.test_the_one_org_rule_holds_through_the_sync_route import C, NAMES, key, seed, send, sync_body

OLD, NEW = "oak_addon_old", "oak_addon_new"
OWN_EXT, OWN_SLUG = NAMES["own"]


def add_on(key_id: str) -> dict:
    return key(key_id, C, extension_install_id=7)


async def owner_of(db: Any, slug: str) -> Optional[str]:
    row = (await db.execute(select(Organization.api_key_id).where(Organization.slug == slug))).first()
    return row[0] if row else "absent"


async def synced(api_key: dict, db: Any, orgs: List[Any]) -> int:
    response = await send(api_key, db, sync_body(orgs, []))
    assert response.status_code == 200, response.text
    return response.json()["organizations_synced"]


@pytest.mark.parametrize("claims", [((NEW, OLD),), ((NEW, None),), ()],
                         ids=["inside-its-grace", "rotated-with-no-grace", "installation-deleted"])
async def test_a_replaced_credential_creates_no_organization(claims: tuple) -> None:
    db = installation_rows(sqlite_session(*org_tables()), *claims)
    assert await synced(add_on(OLD), db, [("ext-fresh", "fresh-org")]) == 0
    assert await owner_of(db, "fresh-org") == "absent"


async def test_a_replaced_credential_does_not_adopt_an_unclaimed_organization() -> None:
    db = installation_rows(sqlite_session(*org_tables()), (NEW, OLD))
    await seed(db, "own", owner=None)
    assert await synced(add_on(OLD), db, ["own"]) == 0
    assert await owner_of(db, OWN_SLUG) is None
    assert await synced(add_on(NEW), db, ["own"]) == 1
    assert await owner_of(db, OWN_SLUG) == NEW


async def test_a_replaced_credential_does_not_relink_a_retired_identity() -> None:
    db = installation_rows(sqlite_session(*org_tables()), (NEW, OLD))
    await seed(db, "own", owner=None, ext="ext-retired")
    assert await synced(add_on(OLD), db, [("ext-reborn", OWN_SLUG)]) == 0
    assert await owner_of(db, OWN_SLUG) is None
    assert await synced(add_on(NEW), db, [("ext-reborn", OWN_SLUG)]) == 1
    assert await owner_of(db, OWN_SLUG) == NEW


async def test_the_current_credential_still_claims() -> None:
    db = installation_rows(sqlite_session(*org_tables()), (NEW, OLD))
    assert await synced(add_on(NEW), db, [("ext-fresh", "fresh-org")]) == 1
    assert await owner_of(db, "fresh-org") == NEW


async def test_a_key_that_is_not_an_add_on_never_asks() -> None:
    # There is no extension_installations table, so asking would fail; an integration key never asks.
    db = sqlite_session(*org_tables())
    assert await synced(key("oak_integration", C), db, [("ext-fresh", "fresh-org")]) == 1


async def test_an_add_on_decides_each_organization_under_its_key_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    taken: List[str] = []
    real = integrations.serialize_key_ownership

    @asynccontextmanager
    async def _seam(db: Any, key_id: str) -> AsyncIterator[None]:
        taken.append(key_id)
        async with real(db, key_id):
            yield

    monkeypatch.setattr(integrations, "serialize_key_ownership", _seam)
    db = installation_rows(sqlite_session(*org_tables()), (NEW, None))
    assert await synced(add_on(NEW), db, [("ext-a", "org-a"), ("ext-b", "org-b")]) == 2
    assert taken and set(taken) == {NEW}
    taken.clear()
    assert await synced(key("oak_integration", C), db, [("ext-c", "org-c")]) == 1
    assert taken == []


class _CurrentOrNot(_SlugTakenSession):
    """Every lookup misses, so a current credential reaches the insert and its flush fails as a slug clash."""


@pytest.mark.parametrize("current, status, error", [(False, 409, "superseded_credential"), (True, 409, None)],
                         ids=["replaced", "current"])
def test_provisioning_with_a_replaced_credential_creates_nothing(
    monkeypatch: pytest.MonkeyPatch, current: bool, status: int, error: Optional[str]
) -> None:
    async def _registered() -> frozenset:
        return frozenset({"nuc2"})

    async def _current(_db: Any, _key_id: str) -> bool:
        return current

    monkeypatch.setattr(integrations, "_refuse_an_unregistered_cluster", lambda _c: _registered())
    monkeypatch.setattr(integrations, "is_current_add_on_credential", _current)
    db = _CurrentOrNot()
    api_key = {"id": OLD, "created_by": "ops@example.com", "extension_install_id": 7,
               "cluster_permissions": [{"cluster_id": "nuc2", "projects": ["*"]}]}
    with pytest.raises(HTTPException) as refused:
        asyncio.run(provision_tenant_access(body=_Body(), api_key=api_key, db=db))
    assert refused.value.status_code == status
    if error:
        assert refused.value.detail["error"] == error and db.added == []
    else:
        assert db.added, "a current credential did not reach the insert"


def test_every_claim_arm_asks_first() -> None:
    source = inspect.getsource(integrations._upsert_synced_organizations)
    lines = source.splitlines()
    claims = [n for n, line in enumerate(lines) if re.search(
        r"existing\.api_key_id = api_key_id|db\.add\(Organization\(|slug_owner\.api_key_id = api_key_id", line)]
    asks = [n for n, line in enumerate(lines) if "await _replaced()" in line]
    assert len(claims) == 3 and len(asks) == 3
    assert all(any(ask < claim for ask in asks) for claim in claims)
    provision = inspect.getsource(integrations.provision_tenant_access)
    assert provision.index("is_current_add_on_credential") < provision.index("org = Organization(")
