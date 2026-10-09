"""An owner key of an organization another owner key still serves can be ended; the last able owner cannot.

Revoking or deleting a key takes it out of every owner set another able owner carries on, and moves the
primary (the key OrcaHub callbacks use) to the oldest able owner left. An organization whose other
owners are all ended or unreadable still refuses the end, as a sole owner's does. Two revokes of a set's
two keys cannot both pass: the organization lock makes the second see the first.

Also the Administrator's owner-set routes (add, remove, make primary), which do not exist unless
INTEGRATION_MULTI_OWNER_ENABLED is on, and what the key list, the organization list and a transfer say
about a set.
"""

import asyncio
from typing import Any, Dict, List, Optional

import httpx
import pytest
from sqlalchemy import select

from app.core import config, dependencies
from app.core.vault_client import VaultClientError
from app.models.db_models import Organization
from app.routers import integrations
from app.services import activity_logger as activity_logger_module
from app.services import organization_owners
from app.services.integration_key_ownership import OWNERSHIP_UNKNOWN_DETAIL
from app.services.organization_owners import MAX_OWNER_KEYS, add_owner, owner_key_ids
from tests._owner_invariants import assert_owner_invariants
from tests._owner_key_world import RecordingLogger
from tests.test_a_key_that_owns_an_organization_is_not_revoked_or_deleted import (  # noqa: F401
    ADMIN,
    KEYS,
    PARTNER,
    PLAIN,
    TENANT,
    app_for,
    denial,
    key,
    own,
    owned,
    path,
    send,
)

CO, THIRD = "oak_co_owner", "oak_third_owner"
ORGS = "/api/v1/integrations/organizations"
PAST = "2026-01-01T00:00:00"
ADD, REMOVE, PRIMARY = (organization_owners.ADD_ACTION, organization_owners.REMOVE_ACTION,
                        organization_owners.PRIMARY_ACTION)


def put_key(world: Dict[str, Any], metadata: Dict[str, Any]) -> None:
    world["vault"].docs[path(metadata["id"])] = dict(metadata)
    world["vault"].docs[path(metadata["id"], "secret")] = {"hash": "h", "raw": "oas_x"}


def org_id(db: Any, slug: str) -> int:
    return db.sync.execute(select(Organization.id).where(Organization.slug == slug)).scalar_one()


@pytest.fixture
def co_owned(owned: Dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:  # noqa: F811
    """PLAIN owns acme-cloud and nimbus-co as their primary. CO and THIRD are active OrcaHub keys that own
    nothing yet. Every owner-set change is recorded in `owner_log`."""
    put_key(owned, key(CO))
    put_key(owned, key(THIRD))
    records = RecordingLogger()
    monkeypatch.setattr(activity_logger_module, "get_activity_logger", lambda: records)
    db = owned["db"]
    return {**owned, "acme": org_id(db, "acme-cloud"), "nimbus": org_id(db, "nimbus-co"), "owner_log": records}


@pytest.fixture
def multi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(integrations, "multi_owner_enabled", lambda: True)


async def co_own(db: Any, org: int, key_id: str) -> None:
    organization = db.sync.get(Organization, org)
    assert await add_owner(db, organization, key_id, via="admin", by="admin@example.test")
    await db.commit()


def primary(db: Any, org: int) -> Optional[str]:
    db.sync.expire_all()
    return db.sync.get(Organization, org).api_key_id


def actions(world: Dict[str, Any]) -> List[str]:
    return [call["action"] for call in world["owner_log"].calls]


def unreadable(world: Dict[str, Any], key_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Vault answers 503 for this key's metadata: the key may be fine, but nothing can say so."""
    real = world["vault"].vault_read

    def read(at: str, use_cache: bool = True) -> Dict[str, Any]:
        if at == path(key_id):
            raise VaultClientError("Vault read failed: 503 Server Error", status_code=503)
        return real(at, use_cache)

    monkeypatch.setattr(world["vault"], "vault_read", read)


# note: ending a key that co-owns

@pytest.mark.parametrize("method, suffix, trigger", [("POST", "/revoke", "revoke"), ("DELETE", "", "delete")],
                         ids=["revoke", "delete"])
async def test_a_co_owned_primary_ends_and_the_co_owner_takes_over(
    co_owned: Dict[str, Any], method: str, suffix: str, trigger: str
) -> None:
    db = co_owned["db"]
    for org in (co_owned["acme"], co_owned["nimbus"]):
        await co_own(db, org, CO)
    response = await send(co_owned, ADMIN, method, f"{KEYS}/{PLAIN}{suffix}")
    assert response.status_code == 200, response.text
    for org in (co_owned["acme"], co_owned["nimbus"]):
        assert await owner_key_ids(db, org) == [CO]
        assert primary(db, org) == CO
    if method == "POST":
        assert co_owned["vault"].docs[path(PLAIN)]["is_revoked"] is True
    else:
        assert path(PLAIN) not in co_owned["vault"].docs
    assert PLAIN in co_owned["evicted"]
    assert actions(co_owned) == [REMOVE] * 2
    record = co_owned["owner_log"].calls[0]
    assert (record["details"]["owner_key_ids_before"], record["details"]["owner_key_ids_after"]) == ([PLAIN, CO], [CO])
    assert (record["details"]["owner_key_id"], record["details"]["trigger"], record["details"]["actor_email"]) == (
        PLAIN, trigger, ADMIN.email)
    assert (record["before_state"]["owner_key_id"], record["after_state"]["owner_key_id"]) == (PLAIN, CO)
    (ended,) = co_owned["log"].calls
    assert ended["result"] == "success" and ended["target_id"] == PLAIN
    await assert_owner_invariants(db)


async def test_a_partner_ends_a_co_owned_key_it_made(co_owned: Dict[str, Any]) -> None:
    """The co-owner carries on for a partner's revoke too: the rule is about the organizations, not the caller."""
    db = co_owned["db"]
    for org in (co_owned["acme"], co_owned["nimbus"]):
        await co_own(db, org, CO)
    response = await send(co_owned, PARTNER, "POST", f"{KEYS}/{PLAIN}/revoke")
    assert response.status_code == 200, response.text
    assert primary(db, co_owned["acme"]) == CO and primary(db, co_owned["nimbus"]) == CO
    assert {call["details"]["actor_email"] for call in co_owned["owner_log"].calls} == {PARTNER.email}
    await assert_owner_invariants(db)


async def test_the_sole_owned_organization_still_refuses(co_owned: Dict[str, Any]) -> None:
    db = co_owned["db"]
    await co_own(db, co_owned["acme"], CO)
    response = await send(co_owned, ADMIN, "POST", f"{KEYS}/{PLAIN}/revoke")
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert (detail["organization_count"], detail["organization_slugs"]) == (1, ["nimbus-co"])
    assert detail["remedies"] == ["transfer_owner", "delete_organization", "rotate_secret"]
    assert "co_owned_organization_count" not in detail, "no co-owner remedy while the flag is off"
    assert detail["message"].startswith("This key owns 1 OrcaHub organization (nimbus-co).")
    assert await owner_key_ids(db, co_owned["acme"]) == [PLAIN, CO], "nothing moved"
    assert primary(db, co_owned["acme"]) == PLAIN
    assert co_owned["vault"].writes == [] and co_owned["evicted"] == [] and co_owned["owner_log"].calls == []
    assert denial(co_owned)["details"]["owned_organization_count"] == 1


async def test_with_multi_owner_on_the_refusal_offers_a_co_owner(
    co_owned: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.integration_key_ownership.multi_owner_enabled", lambda: True)
    await co_own(co_owned["db"], co_owned["acme"], CO)
    detail = (await send(co_owned, ADMIN, "POST", f"{KEYS}/{PLAIN}/revoke")).json()["detail"]
    assert detail["remedies"][-1] == "add_owner_key" and detail["co_owned_organization_count"] == 1
    assert detail["message"].startswith("This key is the only able owner of 1 OrcaHub organization (nimbus-co).")
    assert "co-owner" in detail["message"]


@pytest.mark.parametrize("co_owner", [
    key(CO, is_revoked=True, is_active=False),
    key(CO, is_active=False),
    key(CO, expires_at=PAST),
    key(CO, app_type="automation"),
    None,
], ids=["revoked", "inactive", "expired", "not_orcahub", "deleted"])
async def test_a_co_owner_that_cannot_sync_does_not_count(
    co_owned: Dict[str, Any], co_owner: Optional[Dict[str, Any]]
) -> None:
    db = co_owned["db"]
    for org in (co_owned["acme"], co_owned["nimbus"]):
        await co_own(db, org, CO)
    if co_owner is None:
        del co_owned["vault"].docs[path(CO)]
    else:
        co_owned["vault"].docs[path(CO)] = co_owner
    response = await send(co_owned, ADMIN, "POST", f"{KEYS}/{PLAIN}/revoke")
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["organization_count"] == 2
    assert co_owned["vault"].writes == [] and co_owned["owner_log"].calls == []


async def test_an_unreadable_co_owner_ends_nothing(co_owned: Dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    db = co_owned["db"]
    for org in (co_owned["acme"], co_owned["nimbus"]):
        await co_own(db, org, CO)
    unreadable(co_owned, CO, monkeypatch)
    response = await send(co_owned, ADMIN, "POST", f"{KEYS}/{PLAIN}/revoke")
    assert (response.status_code, response.json()["detail"]) == (503, OWNERSHIP_UNKNOWN_DETAIL)
    assert co_owned["vault"].writes == [] and co_owned["evicted"] == []
    assert await owner_key_ids(db, co_owned["acme"]) == [PLAIN, CO] and primary(db, co_owned["acme"]) == PLAIN


async def test_a_sole_owned_organization_is_named_even_beside_an_unreadable_co_owner(
    co_owned: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A definite 409 names what to fix; the unreadable set is not a reason to hide it behind a 503."""
    await co_own(co_owned["db"], co_owned["acme"], CO)
    unreadable(co_owned, CO, monkeypatch)
    response = await send(co_owned, ADMIN, "POST", f"{KEYS}/{PLAIN}/revoke")
    assert response.status_code == 409
    assert response.json()["detail"]["organization_slugs"] == ["nimbus-co"]
    assert co_owned["vault"].writes == []


async def test_two_revokes_of_a_two_key_set_cannot_both_pass(co_owned: Dict[str, Any]) -> None:
    db = co_owned["db"]
    for org in (co_owned["acme"], co_owned["nimbus"]):
        await co_own(db, org, CO)
    first, second = await asyncio.gather(
        send(co_owned, ADMIN, "POST", f"{KEYS}/{PLAIN}/revoke"),
        send(co_owned, ADMIN, "POST", f"{KEYS}/{CO}/revoke"),
    )
    assert sorted([first.status_code, second.status_code]) == [200, 409]
    for org in (co_owned["acme"], co_owned["nimbus"]):
        assert len(await owner_key_ids(db, org)) == 1
    assert sum(1 for k in (PLAIN, CO) if co_owned["vault"].docs[path(k)]["is_revoked"]) == 1
    await assert_owner_invariants(db)


# note: what the lists and a transfer say about a set

async def test_the_key_list_counts_what_only_this_key_owns(co_owned: Dict[str, Any]) -> None:
    await co_own(co_owned["db"], co_owned["acme"], CO)
    listed = {k["id"]: k for k in (await send(co_owned, ADMIN, "GET", KEYS)).json()["keys"]}
    assert (listed[PLAIN]["owned_organization_count"], listed[PLAIN]["sole_owned_organization_count"]) == (2, 1)
    detail = (await send(co_owned, ADMIN, "GET", f"{KEYS}/{PLAIN}")).json()
    assert detail["sole_owned_organization_count"] == 1


async def test_the_organization_list_names_every_owner_key(co_owned: Dict[str, Any]) -> None:
    await co_own(co_owned["db"], co_owned["acme"], CO)
    rows = {o["slug"]: o for o in (await send(co_owned, ADMIN, "GET", ORGS)).json()}
    entries = rows["acme-cloud"]["owner_keys"]
    assert [(e["api_key_id"], e["is_primary"], e["state"], e["name"]) for e in entries] == [
        (PLAIN, True, "active", f"{PLAIN} connection"), (CO, False, "active", f"{CO} connection")]
    assert entries[1]["added_via"] == "admin" and entries[1]["added_at"]
    assert [e["api_key_id"] for e in rows["nimbus-co"]["owner_keys"]] == [PLAIN]


async def test_a_partner_outside_the_home_sees_no_owner_keys(co_owned: Dict[str, Any]) -> None:
    """The partner belongs to acme-cloud, but no sale makes it a partner of any owner key's home."""
    await co_own(co_owned["db"], co_owned["acme"], CO)
    rows = {o["slug"]: o for o in (await send(co_owned, PARTNER, "GET", ORGS)).json()}
    assert rows["acme-cloud"]["owner_keys"] == [] and rows["acme-cloud"]["owner_key_name"] is None


async def test_a_transfer_replaces_the_whole_owner_set(co_owned: Dict[str, Any]) -> None:
    db, org = co_owned["db"], co_owned["acme"]
    await co_own(db, org, CO)
    response = await send(co_owned, ADMIN, "PUT", f"{ORGS}/{org}/owner-key", {"api_key_id": THIRD, "reason": "r"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["previous_api_key_id"], body["api_key_id"], body["removed_owner_key_ids"]) == (PLAIN, THIRD, [CO])
    assert await owner_key_ids(db, org) == [THIRD] and primary(db, org) == THIRD
    assert {PLAIN, THIRD, CO} <= set(co_owned["evicted"])
    (record,) = [c for c in co_owned["log"].calls if c["action"] == "integration.organization.owner.transfer"]
    assert record["before_state"] == {"owner_key_id": PLAIN, "owner_key_ids": [PLAIN, CO]}
    assert record["after_state"] == {"owner_key_id": THIRD, "owner_key_ids": [THIRD]}
    await assert_owner_invariants(db)


async def test_a_transfer_to_the_primary_still_drops_its_co_owners(co_owned: Dict[str, Any]) -> None:
    db, org = co_owned["db"], co_owned["acme"]
    await co_own(db, org, CO)
    response = await send(co_owned, ADMIN, "PUT", f"{ORGS}/{org}/owner-key", {"api_key_id": PLAIN, "reason": "r"})
    assert response.status_code == 200, response.text
    assert (response.json()["changed"], response.json()["removed_owner_key_ids"]) == (True, [CO])
    assert await owner_key_ids(db, org) == [PLAIN]
    again = await send(co_owned, ADMIN, "PUT", f"{ORGS}/{org}/owner-key", {"api_key_id": PLAIN, "reason": "r"})
    assert (again.json()["changed"], again.json()["removed_owner_key_ids"]) == (False, [])


# note: the flag

def test_the_flag_is_off_unless_its_variable_says_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INTEGRATION_MULTI_OWNER_ENABLED", raising=False)
    assert config.Settings(_env_file=None).integration_multi_owner_enabled is False
    monkeypatch.setenv("INTEGRATION_MULTI_OWNER_ENABLED", "true")
    assert config.Settings(_env_file=None).integration_multi_owner_enabled is True
    monkeypatch.setenv("INTEGRATION_MULTI_OWNER_ENABLED", "false")
    assert config.Settings(_env_file=None).integration_multi_owner_enabled is False


def test_the_routes_follow_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    on = config.get_settings().model_copy(update={"integration_multi_owner_enabled": True})
    monkeypatch.setattr(config, "get_settings", lambda: on)
    assert organization_owners.multi_owner_enabled() is True
    off = on.model_copy(update={"integration_multi_owner_enabled": False})
    monkeypatch.setattr(config, "get_settings", lambda: off)
    assert organization_owners.multi_owner_enabled() is False


# note: the Administrator's owner-set routes

FLAGGED = [
    ("POST", "/{org}/owner-keys", {"api_key_id": CO, "reason": "r"}),
    ("DELETE", "/{org}/owner-keys/" + CO + "?reason=r", None),
    ("PUT", "/{org}/primary-key", {"api_key_id": CO, "reason": "r"}),
]
FLAGGED_IDS = ["add", "remove", "primary"]


@pytest.mark.parametrize("method, at, body", [
    *FLAGGED,
    ("DELETE", "/{org}/owner-keys/" + CO, None),
    ("POST", "/{org}/owner-keys", {}),
], ids=[*FLAGGED_IDS, "remove_without_reason", "add_without_body"])
@pytest.mark.parametrize("who", ["admin", "tenant"])
async def test_the_owner_set_routes_do_not_exist_while_the_flag_is_off(
    co_owned: Dict[str, Any], method: str, at: str, body: Any, who: str
) -> None:
    """404 before the login, the read-only check and the body, as for a path that does not exist."""
    user = {"admin": ADMIN, "tenant": TENANT}[who]
    response = await send(co_owned, user, method, ORGS + at.format(org=co_owned["acme"]), body)
    assert (response.status_code, response.json()) == (404, {"detail": "Not Found"})
    assert await owner_key_ids(co_owned["db"], co_owned["acme"]) == [PLAIN]
    assert co_owned["owner_log"].calls == [] and co_owned["denials"].calls == []


@pytest.mark.parametrize("method, at", [("POST", "/{org}/owner-keys"), ("PUT", "/{org}/primary-key")],
                         ids=["add", "primary"])
async def test_malformed_json_is_404_while_the_flag_is_off(
    co_owned: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, method: str, at: str
) -> None:
    """FastAPI parses a declared body before any dependency, so these routes read theirs themselves."""
    async def raw(content: bytes) -> httpx.Response:
        transport = httpx.ASGITransport(app=app_for(ADMIN, co_owned["db"]))
        async with httpx.AsyncClient(transport=transport, base_url="http://orcastra.test") as client:
            return await client.request(method, ORGS + at.format(org=co_owned["acme"]), content=content,
                                        headers={"content-type": "application/json"})

    assert (await raw(b'{"api_key_id": ')).status_code == 404
    monkeypatch.setattr(integrations, "multi_owner_enabled", lambda: True)
    malformed = await raw(b'{"api_key_id": ')
    assert malformed.status_code == 422 and malformed.json()["detail"][0]["type"] == "json_invalid"
    missing = await raw(b'{"api_key_id": "oak_x"}')
    assert missing.status_code == 422 and missing.json()["detail"][0]["loc"] == ["body", "reason"]
    bad = await raw(b'{"api_key_id": "oak secret value!", "reason": "r"}')
    assert bad.status_code == 422 and "oak secret value!" not in bad.text, "the 422 never echoes the input"


async def test_an_admin_adds_makes_primary_and_removes_an_owner(co_owned: Dict[str, Any], multi: None) -> None:
    db, org = co_owned["db"], co_owned["acme"]
    added = await send(co_owned, ADMIN, "POST", f"{ORGS}/{org}/owner-keys",
                       {"api_key_id": CO, "reason": "second connection"})
    assert added.status_code == 200, added.text
    assert added.json() == {"organization_id": org, "organization_slug": "acme-cloud", "external_id": "ext-acme-cloud",
                            "primary_key_id": PLAIN, "owner_key_ids": [PLAIN, CO], "changed": True}
    again = await send(co_owned, ADMIN, "POST", f"{ORGS}/{org}/owner-keys", {"api_key_id": CO, "reason": "again"})
    assert again.status_code == 200 and again.json()["changed"] is False
    made = await send(co_owned, ADMIN, "PUT", f"{ORGS}/{org}/primary-key", {"api_key_id": CO, "reason": "callbacks"})
    assert made.status_code == 200 and made.json()["primary_key_id"] == CO
    removed = await send(co_owned, ADMIN, "DELETE", f"{ORGS}/{org}/owner-keys/{PLAIN}?reason=retired")
    assert removed.status_code == 200 and removed.json()["owner_key_ids"] == [CO]
    last = await send(co_owned, ADMIN, "DELETE", f"{ORGS}/{org}/owner-keys/{CO}?reason=oops")
    assert last.status_code == 409 and last.json()["detail"]["error"] == "last_owner"
    assert actions(co_owned) == [ADD, PRIMARY, REMOVE]
    add_record, primary_record, remove_record = co_owned["owner_log"].calls
    assert (add_record["details"]["reason"], add_record["details"]["actor_email"]) == ("second connection", ADMIN.email)
    assert add_record["details"]["owner_key_ids_after"] == [PLAIN, CO]
    assert (primary_record["before_state"]["owner_key_id"], primary_record["after_state"]["owner_key_id"]) == (PLAIN, CO)
    assert (remove_record["details"]["owner_key_id"], remove_record["details"]["owner_key_ids_after"]) == (PLAIN, [CO])
    assert ("api_responses", "integrations:api_keys:list*") in co_owned["cache"].cleared
    assert set(co_owned["evicted"]) == {PLAIN, CO}
    assert primary(db, org) == CO and await owner_key_ids(db, co_owned["nimbus"]) == [PLAIN]
    await assert_owner_invariants(db)


async def test_removing_a_co_owner_keeps_the_primary(co_owned: Dict[str, Any], multi: None) -> None:
    db, org = co_owned["db"], co_owned["acme"]
    await co_own(db, org, CO)
    response = await send(co_owned, ADMIN, "DELETE", f"{ORGS}/{org}/owner-keys/{CO}?reason=retired")
    assert response.status_code == 200, response.text
    assert (response.json()["primary_key_id"], response.json()["owner_key_ids"]) == (PLAIN, [PLAIN])
    absent = await send(co_owned, ADMIN, "DELETE", f"{ORGS}/{org}/owner-keys/{CO}?reason=again")
    assert absent.status_code == 200 and absent.json()["changed"] is False
    assert actions(co_owned) == [REMOVE]


async def test_removing_the_primary_promotes_the_oldest_able_owner(co_owned: Dict[str, Any], multi: None) -> None:
    db, org = co_owned["db"], co_owned["acme"]
    await co_own(db, org, CO)
    await co_own(db, org, THIRD)
    co_owned["vault"].docs[path(CO)] = key(CO, is_revoked=True, is_active=False)
    response = await send(co_owned, ADMIN, "DELETE", f"{ORGS}/{org}/owner-keys/{PLAIN}?reason=retired")
    assert response.status_code == 200, response.text
    assert response.json()["primary_key_id"] == THIRD, "the revoked co-owner is skipped"
    assert response.json()["owner_key_ids"] == [CO, THIRD]
    await assert_owner_invariants(db)


async def test_removing_the_primary_needs_an_able_owner_left(
    co_owned: Dict[str, Any], multi: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, org = co_owned["db"], co_owned["acme"]
    await co_own(db, org, CO)
    co_owned["vault"].docs[path(CO)] = key(CO, is_revoked=True, is_active=False)
    refused = await send(co_owned, ADMIN, "DELETE", f"{ORGS}/{org}/owner-keys/{PLAIN}?reason=retired")
    assert refused.status_code == 409 and refused.json()["detail"]["error"] == "no_able_owner"
    co_owned["vault"].docs[path(CO)] = key(CO)
    unreadable(co_owned, CO, monkeypatch)
    unknown = await send(co_owned, ADMIN, "DELETE", f"{ORGS}/{org}/owner-keys/{PLAIN}?reason=retired")
    assert (unknown.status_code, unknown.json()["detail"]) == (503, OWNERSHIP_UNKNOWN_DETAIL)
    assert await owner_key_ids(db, org) == [PLAIN, CO] and primary(db, org) == PLAIN
    assert co_owned["owner_log"].calls == [] and co_owned["evicted"] == []


@pytest.mark.parametrize("target, metadata, code", [
    ("oak_missing", None, "target_key_missing"),
    (CO, key(CO, is_revoked=True, is_active=False), "target_key_revoked"),
    (CO, key(CO, expires_at=PAST), "target_key_expired"),
    (CO, key(CO, is_active=False), "target_key_inactive"),
    (CO, key(CO, app_type="automation"), "target_key_not_orcahub"),
], ids=["missing", "revoked", "expired", "inactive", "not_orcahub"])
async def test_an_admin_cannot_add_a_key_that_could_not_own(
    co_owned: Dict[str, Any], multi: None, target: str, metadata: Optional[Dict[str, Any]], code: str
) -> None:
    if metadata is not None:
        co_owned["vault"].docs[path(CO)] = metadata
    response = await send(co_owned, ADMIN, "POST", f"{ORGS}/{co_owned['acme']}/owner-keys",
                          {"api_key_id": target, "reason": "r"})
    assert response.status_code == 409 and response.json()["detail"]["error"] == code
    assert await owner_key_ids(co_owned["db"], co_owned["acme"]) == [PLAIN]
    assert co_owned["owner_log"].calls == []


async def test_an_unreadable_key_is_not_added(
    co_owned: Dict[str, Any], multi: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    unreadable(co_owned, CO, monkeypatch)
    response = await send(co_owned, ADMIN, "POST", f"{ORGS}/{co_owned['acme']}/owner-keys",
                          {"api_key_id": CO, "reason": "r"})
    assert response.status_code == 503 and "nothing changed" in response.json()["detail"]
    assert await owner_key_ids(co_owned["db"], co_owned["acme"]) == [PLAIN]


async def test_an_owner_set_holds_at_most_five_keys(co_owned: Dict[str, Any], multi: None) -> None:
    extra = [f"oak_extra_{n}" for n in range(MAX_OWNER_KEYS)]
    for key_id in extra:
        put_key(co_owned, key(key_id))
    for key_id in extra[:MAX_OWNER_KEYS - 1]:
        response = await send(co_owned, ADMIN, "POST", f"{ORGS}/{co_owned['acme']}/owner-keys",
                              {"api_key_id": key_id, "reason": "r"})
        assert response.status_code == 200, response.text
    full = await send(co_owned, ADMIN, "POST", f"{ORGS}/{co_owned['acme']}/owner-keys",
                      {"api_key_id": extra[-1], "reason": "r"})
    assert full.status_code == 409 and full.json()["detail"]["error"] == "owner_set_full"
    assert len(await owner_key_ids(co_owned["db"], co_owned["acme"])) == MAX_OWNER_KEYS


async def test_a_primary_must_already_be_an_owner(co_owned: Dict[str, Any], multi: None) -> None:
    response = await send(co_owned, ADMIN, "PUT", f"{ORGS}/{co_owned['acme']}/primary-key",
                          {"api_key_id": CO, "reason": "r"})
    assert response.status_code == 409 and response.json()["detail"]["error"] == "key_not_an_owner"


async def test_a_primary_must_be_able_to_sync(co_owned: Dict[str, Any], multi: None) -> None:
    db, org = co_owned["db"], co_owned["acme"]
    await co_own(db, org, CO)
    co_owned["vault"].docs[path(CO)] = key(CO, is_revoked=True, is_active=False)
    response = await send(co_owned, ADMIN, "PUT", f"{ORGS}/{org}/primary-key", {"api_key_id": CO, "reason": "r"})
    assert response.status_code == 409 and response.json()["detail"]["error"] == "target_key_revoked"
    assert primary(db, org) == PLAIN
    same = await send(co_owned, ADMIN, "PUT", f"{ORGS}/{org}/primary-key", {"api_key_id": PLAIN, "reason": "r"})
    assert same.status_code == 200 and same.json()["changed"] is False
    assert co_owned["owner_log"].calls == []


@pytest.mark.parametrize("method, at, body", FLAGGED, ids=FLAGGED_IDS)
@pytest.mark.parametrize("who", ["partner", "tenant"])
async def test_only_an_admin_changes_an_owner_set(
    co_owned: Dict[str, Any], multi: None, method: str, at: str, body: Any, who: str
) -> None:
    db, org = co_owned["db"], co_owned["acme"]
    await co_own(db, org, CO)
    user = {"partner": PARTNER, "tenant": TENANT}[who]
    response = await send(co_owned, user, method, ORGS + at.format(org=org), body)
    assert response.status_code == 403
    assert await owner_key_ids(db, org) == [PLAIN, CO] and primary(db, org) == PLAIN
    assert co_owned["owner_log"].calls == []


@pytest.mark.parametrize("method, at, body", FLAGGED, ids=FLAGGED_IDS)
async def test_read_only_mode_refuses_owner_set_changes(
    co_owned: Dict[str, Any], multi: None, monkeypatch: pytest.MonkeyPatch, method: str, at: str, body: Any
) -> None:
    await co_own(co_owned["db"], co_owned["acme"], CO)
    locked = dependencies.get_settings().model_copy(update={"read_only_mode": True})
    monkeypatch.setattr(dependencies, "get_settings", lambda: locked)
    response = await send(co_owned, ADMIN, method, ORGS + at.format(org=co_owned["acme"]), body)
    assert response.status_code == 403 and "read-only" in response.json()["detail"]
    assert await owner_key_ids(co_owned["db"], co_owned["acme"]) == [PLAIN, CO]


async def test_an_owner_set_belongs_to_a_claimed_synced_organization(co_owned: Dict[str, Any], multi: None) -> None:
    db = co_owned["db"]
    local = own(db, PLAIN, "made-here", source="local")
    unclaimed = own(db, None, "fresh-org")
    body = {"api_key_id": CO, "reason": "r"}
    assert (await send(co_owned, ADMIN, "POST", f"{ORGS}/999999/owner-keys", body)).status_code == 404
    not_synced = await send(co_owned, ADMIN, "POST", f"{ORGS}/{local}/owner-keys", body)
    assert not_synced.status_code == 409 and not_synced.json()["detail"]["error"] == "organization_not_synced"
    no_owner = await send(co_owned, ADMIN, "PUT", f"{ORGS}/{unclaimed}/primary-key", body)
    assert no_owner.status_code == 409 and no_owner.json()["detail"]["error"] == "organization_unclaimed"
    assert co_owned["owner_log"].calls == []
    await assert_owner_invariants(db)
