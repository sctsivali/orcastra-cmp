"""An owner key of an organization another owner key still serves can be ended; the last able owner cannot.

Revoking or deleting a key takes it out of every owner set another able owner carries on, and moves the
primary (the key OrcaHub callbacks use) to the oldest able owner left. Withdrawing a sync capability
keeps the key in the set but moves the primary off it. An organization whose other owners are all ended
or unreadable still refuses the end, as a sole owner's does. Two revokes of a set's two keys cannot
both pass: the organization lock makes the second see the first.

Also the Administrator's owner-set routes (add, remove, make primary), off unless
INTEGRATION_MULTI_OWNER_ENABLED is on.
"""

import asyncio
from typing import Any, Dict, List

import httpx
import pytest

from app.services import activity_logger as activity_logger_module
from app.services.integration_key_ownership import OWNERSHIP_UNKNOWN_DETAIL
from app.services.organization_owners import add_owner, owner_key_ids
from tests._owner_invariants import assert_owner_invariants
from tests.test_a_capability_grant_leaves_a_record import _FakeVault, _RecordingLogger
from tests.test_a_key_that_owns_an_organization_is_not_revoked_or_deleted import own
from tests.test_an_add_on_credential_refuses_grant_edits import (
    ADDON,
    PLAIN,
    installations,
    plain_metadata,
    surface,  # noqa: F401
)
from tests.test_the_add_on_key_rules_hold_through_the_key_routes import ADMIN, KEYS, keys, keys_app, send  # noqa: F401

CO = "oak_co_owner"
ORGS = "/api/v1/integrations/organizations"


def able_metadata(key_id: str, **over: Any) -> Dict[str, Any]:
    return {**plain_metadata(), "id": key_id, "capabilities": ["inventory.read", "org.sync", "tenant.provision"],
            **over}


@pytest.fixture
def co_owned(keys: Dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:  # noqa: F811
    """PLAIN owns acme-cloud (primary) with CO as an able co-owner, and owns globex alone."""
    db = installations((ADDON, None))
    acme = own(db, PLAIN, "acme-cloud")
    globex = own(db, PLAIN, "globex")
    keys["vault"].store[_FakeVault._path(PLAIN, "metadata")] = able_metadata(PLAIN)
    keys["vault"].store[_FakeVault._path(CO, "metadata")] = able_metadata(CO)
    keys["vault"].store[_FakeVault._path(CO, "secret")] = dict(keys["vault"].store[_FakeVault._path(PLAIN, "secret")])
    records = _RecordingLogger()
    monkeypatch.setattr(activity_logger_module, "get_activity_logger", lambda: records)
    keys["scope"](ADMIN)
    return {**keys, "db": db, "acme": acme, "globex": globex, "owner_log": records}


async def co_own(db: Any, org_id: int, key_id: str) -> None:
    from app.models.db_models import Organization

    org = db.sync.get(Organization, org_id)
    assert await add_owner(db, org, key_id, via="admin", by="admin@example.test")
    await db.commit()


def primary(db: Any, org_id: int) -> str:
    from app.models.db_models import Organization

    db.sync.expire_all()
    return db.sync.get(Organization, org_id).api_key_id


def actions(world: Dict[str, Any]) -> List[str]:
    return [call["action"] for call in world["owner_log"].calls]


@pytest.mark.parametrize("method, suffix", [("POST", "/revoke"), ("DELETE", "")], ids=["revoke", "delete"])
async def test_a_co_owned_primary_ends_and_the_co_owner_takes_over(
    co_owned: Dict[str, Any], method: str, suffix: str
) -> None:
    db = co_owned["db"]
    await co_own(db, co_owned["acme"], CO)
    # globex has PLAIN alone, so give it a co-owner too or the end is refused there
    await co_own(db, co_owned["globex"], CO)
    response = await send(ADMIN, db, method, f"{KEYS}/{PLAIN}{suffix}")
    assert response.status_code == 200, response.text
    for org_id in (co_owned["acme"], co_owned["globex"]):
        assert await owner_key_ids(db, org_id) == [CO]
        assert primary(db, org_id) == CO
    assert actions(co_owned) == ["integration.organization.owner.remove"] * 2
    record = co_owned["owner_log"].calls[0]
    assert record["details"]["owner_key_ids_before"] == [PLAIN, CO]
    assert record["details"]["owner_key_ids_after"] == [CO]
    assert record["after_state"]["owner_key_id"] == CO
    await assert_owner_invariants(db)


async def test_the_sole_owned_organization_still_refuses(co_owned: Dict[str, Any]) -> None:
    db = co_owned["db"]
    await co_own(db, co_owned["acme"], CO)
    response = await send(ADMIN, db, "POST", f"{KEYS}/{PLAIN}/revoke")
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert (detail["organization_count"], detail["organization_slugs"]) == (1, ["globex"])
    assert detail["remedies"] == ["transfer_owner", "delete_organization", "rotate_secret"]
    assert await owner_key_ids(db, co_owned["acme"]) == [PLAIN, CO], "nothing moved"
    assert co_owned["vault"].writes == []


async def test_with_multi_owner_on_the_refusal_offers_a_co_owner(
    co_owned: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.integration_key_ownership.multi_owner_enabled", lambda: True)
    db = co_owned["db"]
    await co_own(db, co_owned["acme"], CO)
    detail = (await send(ADMIN, db, "POST", f"{KEYS}/{PLAIN}/revoke")).json()["detail"]
    assert detail["remedies"][-1] == "add_owner_key" and detail["co_owned_organization_count"] == 1
    assert detail["message"].startswith("This key is the only able owner of 1 OrcaHub organization (globex).")
    assert "co-owner" in detail["message"]


@pytest.mark.parametrize("co_owner", [
    able_metadata(CO, is_revoked=True, is_active=False),
    able_metadata(CO, capabilities=["inventory.read"]),
    able_metadata(CO, app_type="automation"),
], ids=["revoked", "cannot_sync", "not_orcahub"])
async def test_a_co_owner_that_cannot_sync_does_not_count(co_owned: Dict[str, Any], co_owner: Dict[str, Any]) -> None:
    db = co_owned["db"]
    await co_own(db, co_owned["acme"], CO)
    await co_own(db, co_owned["globex"], CO)
    co_owned["vault"].store[_FakeVault._path(CO, "metadata")] = co_owner
    response = await send(ADMIN, db, "POST", f"{KEYS}/{PLAIN}/revoke")
    assert response.status_code == 409
    assert response.json()["detail"]["organization_count"] == 2


async def test_an_unreadable_co_owner_ends_nothing(co_owned: Dict[str, Any]) -> None:
    db = co_owned["db"]
    await co_own(db, co_owned["acme"], CO)
    await co_own(db, co_owned["globex"], CO)
    del co_owned["vault"].store[_FakeVault._path(CO, "metadata")]
    response = await send(ADMIN, db, "POST", f"{KEYS}/{PLAIN}/revoke")
    assert (response.status_code, response.json()["detail"]) == (503, OWNERSHIP_UNKNOWN_DETAIL)
    assert co_owned["vault"].writes == [] and await owner_key_ids(db, co_owned["acme"]) == [PLAIN, CO]


async def test_withdrawing_org_sync_keeps_the_key_but_moves_the_primary(co_owned: Dict[str, Any]) -> None:
    db = co_owned["db"]
    await co_own(db, co_owned["acme"], CO)
    await co_own(db, co_owned["globex"], CO)
    response = await send(ADMIN, db, "PUT", f"{KEYS}/{PLAIN}/capabilities",
                          {"capabilities": ["inventory.read", "tenant.provision"]})
    assert response.status_code == 200, response.text
    assert await owner_key_ids(db, co_owned["acme"]) == [PLAIN, CO]
    assert primary(db, co_owned["acme"]) == CO and primary(db, co_owned["globex"]) == CO
    assert actions(co_owned) == ["integration.organization.owner.primary"] * 2
    await assert_owner_invariants(db)


async def test_two_revokes_of_a_two_key_set_cannot_both_pass(co_owned: Dict[str, Any], monkeypatch) -> None:
    # Co-owners own the organization only while owner sets are on.
    monkeypatch.setattr("app.services.organization_owners.multi_owner_enabled", lambda: True)
    db = co_owned["db"]
    await co_own(db, co_owned["acme"], CO)
    await co_own(db, co_owned["globex"], CO)
    first, second = await asyncio.gather(
        send(ADMIN, db, "POST", f"{KEYS}/{PLAIN}/revoke"),
        send(ADMIN, db, "POST", f"{KEYS}/{CO}/revoke"),
    )
    assert sorted([first.status_code, second.status_code]) == [200, 409]
    for org_id in (co_owned["acme"], co_owned["globex"]):
        assert len(await owner_key_ids(db, org_id)) == 1
    await assert_owner_invariants(db)


# The Administrator's owner-set routes.

@pytest.fixture
def multi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.routers.integrations.multi_owner_enabled", lambda: True)


# Removing an owner row is not listed: it answers with the setting off too, so rows enrolled while
# it was on can be cleaned up (test_multi_owner_off_means_one_owner.py).
@pytest.mark.parametrize("method, path, body", [
    ("POST", "/{org}/owner-keys", {"api_key_id": CO, "reason": "r"}),
    ("PUT", "/{org}/primary-key", {"api_key_id": CO, "reason": "r"}),
], ids=["add", "primary"])
async def test_the_owner_set_routes_do_not_exist_while_the_flag_is_off(
    co_owned: Dict[str, Any], method: str, path: str, body: Any
) -> None:
    response = await send(ADMIN, co_owned["db"], method, ORGS + path.format(org=co_owned["acme"]), body)
    assert response.status_code == 404


@pytest.mark.parametrize("method, path", [("POST", "/{org}/owner-keys"), ("PUT", "/{org}/primary-key")],
                         ids=["add", "primary"])
async def test_malformed_json_is_404_while_the_flag_is_off(
    co_owned: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    """FastAPI parses a declared body before any dependency, so these routes read theirs themselves."""
    async def raw(content: bytes) -> httpx.Response:
        app = keys_app(ADMIN, co_owned["db"])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://orcastra.test") as client:
            return await client.request(method, ORGS + path.format(org=co_owned["acme"]), content=content,
                                        headers={"content-type": "application/json"})

    assert (await raw(b'{"api_key_id": ')).status_code == 404
    monkeypatch.setattr("app.routers.integrations.multi_owner_enabled", lambda: True)
    malformed = await raw(b'{"api_key_id": ')
    assert malformed.status_code == 422 and malformed.json()["detail"][0]["type"] == "json_invalid"
    missing = await raw(b'{"api_key_id": "oak_x"}')
    assert missing.status_code == 422 and missing.json()["detail"][0]["loc"] == ["body", "reason"]


async def test_an_admin_adds_makes_primary_and_removes_an_owner(co_owned: Dict[str, Any], multi: None) -> None:
    db, org = co_owned["db"], co_owned["acme"]
    added = await send(ADMIN, db, "POST", f"{ORGS}/{org}/owner-keys", {"api_key_id": CO, "reason": "second connection"})
    assert added.status_code == 200, added.text
    assert added.json() == {"organization_id": org, "organization_slug": "acme-cloud", "external_id": "ext-acme-cloud",
                                 "primary_key_id": PLAIN, "owner_key_ids": [PLAIN, CO], "changed": True}
    again = await send(ADMIN, db, "POST", f"{ORGS}/{org}/owner-keys", {"api_key_id": CO, "reason": "again"})
    assert again.json()["changed"] is False
    made = await send(ADMIN, db, "PUT", f"{ORGS}/{org}/primary-key", {"api_key_id": CO, "reason": "callbacks"})
    assert made.status_code == 200 and made.json()["primary_key_id"] == CO
    removed = await send(ADMIN, db, "DELETE", f"{ORGS}/{org}/owner-keys/{PLAIN}?reason=retired")
    assert removed.status_code == 200 and removed.json()["owner_key_ids"] == [CO]
    last = await send(ADMIN, db, "DELETE", f"{ORGS}/{org}/owner-keys/{CO}?reason=oops")
    assert last.status_code == 409 and last.json()["detail"]["error"] == "last_owner"
    assert actions(co_owned) == ["integration.organization.owner.add", "integration.organization.owner.primary",
                                 "integration.organization.owner.remove"]
    assert ("api_responses", "integrations:api_keys:list*") in co_owned["cache"].cleared
    await assert_owner_invariants(db)


async def test_removing_the_primary_promotes_the_oldest_able_owner(co_owned: Dict[str, Any], multi: None) -> None:
    db, org = co_owned["db"], co_owned["acme"]
    await co_own(db, org, CO)
    co_owned["vault"].store[_FakeVault._path("oak_third", "metadata")] = able_metadata("oak_third")
    await co_own(db, org, "oak_third")
    co_owned["vault"].store[_FakeVault._path(CO, "metadata")] = able_metadata(CO, is_revoked=True, is_active=False)
    response = await send(ADMIN, db, "DELETE", f"{ORGS}/{org}/owner-keys/{PLAIN}?reason=retired")
    assert response.status_code == 200, response.text
    assert response.json()["primary_key_id"] == "oak_third", "the revoked co-owner is skipped"


@pytest.mark.parametrize("target, code", [
    ("oak_missing", 409),
    (ADDON, 409),
], ids=["missing", "add_on"])
async def test_an_admin_cannot_add_a_key_that_could_not_own(
    co_owned: Dict[str, Any], multi: None, target: str, code: int
) -> None:
    co_owned["vault"].store.pop(_FakeVault._path("oak_missing", "metadata"), None)
    response = await send(ADMIN, co_owned["db"], "POST", f"{ORGS}/{co_owned['acme']}/owner-keys",
                          {"api_key_id": target, "reason": "r"})
    assert response.status_code in (code, 503)
    assert await owner_key_ids(co_owned["db"], co_owned["acme"]) == [PLAIN]


async def test_a_primary_must_already_be_an_owner(co_owned: Dict[str, Any], multi: None) -> None:
    response = await send(ADMIN, co_owned["db"], "PUT", f"{ORGS}/{co_owned['acme']}/primary-key",
                          {"api_key_id": CO, "reason": "r"})
    assert response.status_code == 409 and response.json()["detail"]["error"] == "key_not_an_owner"
