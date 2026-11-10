"""An owner-set change is decided on the organization as it stands once its lock is held.

Sessions do not expire on commit, so an organization a handler loaded before taking the organization
lock keeps the primary it had then. With two concurrent revokes, of a set's primary and of another
owner, the second would skip the promotion the first made necessary and fail on the primary-in-set
foreign key after Vault had been written.

So the lock reads its organizations again, the owner sets are checked before the Vault write, the
key's cached copies are dropped whenever the Vault write may have landed, and the answers are fixed
sentences.
"""

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.db_models import Organization
from app.routers import integrations
from app.services.integration_key_ownership import OWNERSHIP_UNKNOWN_DETAIL
from app.services.organization_owners import owner_key_ids, serialize_org_owners
from tests._owner_invariants import assert_owner_invariants
from tests._sqlite_session import AsyncSessionFacade
from tests.test_a_capability_grant_leaves_a_record import _FakeVault
from tests.test_a_co_owned_organization_survives_its_owner_ending import (  # noqa: F401
    CO,
    ORGS,
    able_metadata,
    co_own,
    co_owned,
    primary,
)
from tests.test_an_add_on_credential_refuses_grant_edits import PLAIN, surface  # noqa: F401
from tests.test_an_owner_key_vouches_for_a_key_it_holds import EXT, NEW, OWNER, OWNER_KEYS, call, enroll, world  # noqa: F401
from tests.test_the_add_on_key_rules_hold_through_the_key_routes import ADMIN, KEYS, keys, send  # noqa: F401

THIRD = "oak_third_owner"
LEAKED = "OperationalError: SQL fk_org_primary_owner_key oak_plain"


@pytest.fixture
def enrolled(world: Dict[str, Any]) -> Dict[str, Any]:  # noqa: F811
    return world


@pytest.fixture
def three(co_owned: Dict[str, Any]) -> Dict[str, Any]:  # noqa: F811
    """acme-cloud and fossgo are each owned by PLAIN (primary), CO and THIRD, all able."""
    co_owned["vault"].store[_FakeVault._path(THIRD, "metadata")] = able_metadata(THIRD)
    return co_owned


async def _co_own_all(owned: Dict[str, Any]) -> None:
    for org_id in (owned["acme"], owned["fossgo"]):
        await co_own(owned["db"], org_id, CO)
        await co_own(owned["db"], org_id, THIRD)


def _second_session(db: Any) -> AsyncSessionFacade:
    """Another request's session on the same database, the way two workers each hold their own."""
    return AsyncSessionFacade(Session(db.sync.get_bind(), expire_on_commit=False))


async def test_the_lock_reads_the_organization_again(three: Dict[str, Any]) -> None:
    db = three["db"]
    await co_own(db, three["acme"], CO)
    org = db.sync.get(Organization, three["acme"])
    db.sync.execute(text("UPDATE organizations SET api_key_id = :key WHERE id = :id"), {"key": CO, "id": org.id})
    db.sync.commit()
    assert org.api_key_id == PLAIN, "the session's copy is from before the change"
    async with serialize_org_owners(db, [org.id]):
        assert org.api_key_id == CO


async def test_a_second_end_decides_on_the_primary_the_first_one_moved(
    three: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revoking CO waits for the organization lock while PLAIN's revoke moves the primary to CO."""
    db = three["db"]
    await _co_own_all(three)
    other = _second_session(db)
    real = integrations.serialize_org_owners
    first: List[Any] = []
    held: List[Organization] = []

    @asynccontextmanager
    async def first_revoke_lands_while_waiting(session: Any, org_ids: Any) -> AsyncIterator[None]:
        if session is other and not first:
            # The second request holds these organizations as it loaded them, with PLAIN as primary.
            held.extend(other.sync.get(Organization, org_id) for org_id in org_ids)
            assert {org.api_key_id for org in held} == {PLAIN}
            first.append(await send(ADMIN, db, "POST", f"{KEYS}/{PLAIN}/revoke"))
        async with real(session, org_ids):
            yield

    monkeypatch.setattr(integrations, "serialize_org_owners", first_revoke_lands_while_waiting)
    second = await send(ADMIN, other, "POST", f"{KEYS}/{CO}/revoke")
    assert first[0].status_code == 200, first[0].text
    assert second.status_code == 200, second.text
    for org_id in (three["acme"], three["fossgo"]):
        assert await owner_key_ids(db, org_id) == [THIRD]
        assert primary(db, org_id) == THIRD
    await assert_owner_invariants(db)


async def test_an_admin_removal_decides_on_the_primary_a_revoke_moved(
    three: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Administrator removes CO, which PLAIN's revoke made primary while the removal waited."""
    monkeypatch.setattr(integrations, "multi_owner_enabled", lambda: True)
    db = three["db"]
    await _co_own_all(three)
    other = _second_session(db)
    real = integrations.serialize_org_owners
    first: List[Any] = []

    @asynccontextmanager
    async def revoke_lands_while_waiting(session: Any, org_ids: Any) -> AsyncIterator[None]:
        if session is other and not first:
            first.append(await send(ADMIN, db, "POST", f"{KEYS}/{PLAIN}/revoke"))
        async with real(session, org_ids):
            yield

    monkeypatch.setattr(integrations, "serialize_org_owners", revoke_lands_while_waiting)
    response = await send(ADMIN, other, "DELETE", f"{ORGS}/{three['acme']}/owner-keys/{CO}?reason=retired")
    assert first[0].status_code == 200, first[0].text
    assert response.status_code == 200, response.text
    assert response.json()["primary_key_id"] == THIRD and response.json()["owner_key_ids"] == [THIRD]
    await assert_owner_invariants(db)


async def test_a_refused_owner_set_writes_nothing_to_vault(
    three: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    db = three["db"]
    await _co_own_all(three)

    async def refused(_db: Any) -> None:
        raise RuntimeError(LEAKED)

    monkeypatch.setattr(integrations, "check_owner_sets", refused)
    response = await send(ADMIN, db, "POST", f"{KEYS}/{PLAIN}/revoke")
    assert (response.status_code, response.json()["detail"]) == (503, OWNERSHIP_UNKNOWN_DETAIL)
    assert three["vault"].writes == [] and three["evicted"] == []
    assert await owner_key_ids(db, three["acme"]) == [PLAIN, CO, THIRD] and primary(db, three["acme"]) == PLAIN


@pytest.mark.parametrize("method, suffix", [("POST", "/revoke"), ("DELETE", "")], ids=["revoke", "delete"])
async def test_a_commit_that_fails_after_vault_still_evicts_and_names_no_database(
    three: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, method: str, suffix: str
) -> None:
    db = three["db"]
    await _co_own_all(three)

    async def lost() -> None:
        raise RuntimeError(LEAKED)

    monkeypatch.setattr(db, "commit", lost)
    response = await send(ADMIN, db, method, f"{KEYS}/{PLAIN}{suffix}")
    assert (response.status_code, response.json()["detail"]) == (503, integrations.OWNER_SETS_NOT_SAVED)
    assert three["evicted"] == [PLAIN], "the key must stop working from the cache once Vault says so"
    assert "OperationalError" not in response.text


async def test_a_vault_write_that_raises_still_evicts_and_names_nothing(
    three: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    db = three["db"]
    await _co_own_all(three)

    def lost(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError(LEAKED)

    monkeypatch.setattr(three["vault"], "vault_kv_put", lost)
    response = await send(ADMIN, db, "POST", f"{KEYS}/{PLAIN}/revoke")
    assert (response.status_code, response.json()["detail"]) == (500, "Could not revoke the API key")
    assert three["evicted"] == [PLAIN], "the write may have landed before its reply was lost"


async def test_a_key_that_left_while_waiting_is_not_removed_again(
    enrolled: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An Administrator's transfer to NEW lands while OWNER's own removal waits for the lock."""
    db = enrolled["db"]
    assert (await call(enrolled, OWNER, "POST", OWNER_KEYS, enroll())).json()["added"] is True
    enrolled["records"].calls.clear()
    real = integrations.serialize_org_owners

    @asynccontextmanager
    async def transfer_lands_first(session: Any, org_ids: Any) -> AsyncIterator[None]:
        session.sync.execute(text("UPDATE organizations SET api_key_id = :key WHERE id = :id"),
                             {"key": NEW, "id": enrolled["org"]})
        session.sync.execute(text("DELETE FROM organization_owner_keys WHERE api_key_id = :key"), {"key": OWNER})
        session.sync.commit()
        async with real(session, org_ids):
            yield

    monkeypatch.setattr(integrations, "serialize_org_owners", transfer_lands_first)
    response = await call(enrolled, OWNER, "DELETE", f"/api/v1/integrations/external/organizations/{EXT}/owner-keys/self")
    assert response.json() == {"removed": False, "primary_key_id": None, "owner_key_ids": None}
    assert enrolled["records"].calls == [], "nothing changed, so nothing is recorded"
    assert await owner_key_ids(db, enrolled["org"]) == [NEW]
    await assert_owner_invariants(db)
