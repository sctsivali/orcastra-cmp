"""A key that owns an OrcaHub organization is not revoked, deleted or stripped of what it syncs with.

Only the owner key syncs an organization, updates its members and policies and provisions for it,
and no other key can take it over. Revoking or deleting the owner therefore froze its
organizations, which is what happened on production on 2026-10-08. The key routes now refuse to
end an owner, for every caller, an Admin included, and say what to do instead: transfer the
organizations, delete them, or rotate the secret (an add-on's credential is suspended instead).

These send the real routes through their RBAC gates against SQLite organization rows and an
in-memory Vault, and assert the status, the structured detail, the audit record, and that
nothing was written, deleted or evicted.
"""

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional

import pytest

from app.models.db_models import Organization, OrganizationMember
from app.routers import integrations
from app.services.integration_key_ownership import OWNERSHIP_UNKNOWN_DETAIL
from tests.test_a_capability_grant_leaves_a_record import PARTNER, _FakeVault
from tests.test_an_add_on_credential_refuses_grant_edits import (
    ADDON,
    PLAIN,
    addon_metadata,
    installations,
    plain_metadata,
    surface,  # noqa: F401
)
from tests.test_the_add_on_key_rules_hold_through_the_key_routes import (
    ADMIN,
    ALIAS_ADMIN,
    KEYS,
    USERS,
    _Down,
    keys,  # noqa: F401
    send,
)

ADMIN_REVOKE = (
    "This key owns 2 OrcaHub organizations (acme-cloud, fossgo). Revoking it would leave them with an "
    "owner no other key can replace, so their sync, member updates and provisioning would stop. "
    "Transfer them to another active key first, or delete them. To stop a leaked secret now, rotate "
    "the secret instead: the key ID and its organizations stay."
)


def own(db: Any, key_id: str, slug: str, *, source: str = "orcahub", active: bool = True) -> int:
    db.add(Organization(name=slug.title(), slug=slug, source=source, external_id=f"ext-{slug}",
                        api_key_id=key_id, created_by="system", is_active=active))
    db.sync.commit()
    return db.sync.query(Organization.id).filter(Organization.slug == slug).scalar()


@pytest.fixture
def owned(keys: Dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:  # noqa: F811
    """PLAIN owns acme-cloud and fossgo. The partner belongs to acme-cloud only."""
    db = installations((ADDON, None))
    acme = own(db, PLAIN, "acme-cloud")
    own(db, PLAIN, "fossgo")
    db.add(OrganizationMember(organization_id=acme, user_email=PARTNER.email, role="partner"))
    db.sync.commit()
    locks: List[str] = []
    real_lock = integrations.serialize_key_ownership

    @asynccontextmanager
    async def _seam(session: Any, key_id: str) -> AsyncIterator[None]:
        locks.append(key_id)
        async with real_lock(session, key_id):
            yield

    monkeypatch.setattr(integrations, "serialize_key_ownership", _seam)
    return {**keys, "db": db, "locks": locks}


def nothing_changed(owned: Dict[str, Any], key_id: str = PLAIN) -> None:
    vault = owned["vault"]
    assert vault.writes == [] and vault.deletes == [] and owned["evicted"] == []
    assert owned["cache"].cleared == []
    assert _FakeVault._path(key_id, "metadata") in vault.store


def denial(owned: Dict[str, Any]) -> Dict[str, Any]:
    (record,) = owned["log"].calls
    assert (record["result"], record["error_code"]) == ("denied", "key_owns_organizations")
    return record


# note: revoke and delete of an owner

@pytest.mark.parametrize("method, suffix, action, audited", [
    ("POST", "/revoke", "revoke", "integration.api_key.revoke"),
    ("DELETE", "", "delete", "integration.api_key.delete"),
], ids=["revoke", "delete"])
@pytest.mark.parametrize("who", ["admin", "alias_admin"])
async def test_an_admin_cannot_end_an_owner_key(
    owned: Dict[str, Any], who: str, method: str, suffix: str, action: str, audited: str
) -> None:
    user = USERS[who]
    owned["scope"](user)
    response = await send(user, owned["db"], method, f"{KEYS}/{PLAIN}{suffix}")
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail == {
        "error": "key_owns_organizations", "action": action,
        "message": ADMIN_REVOKE if action == "revoke" else ADMIN_REVOKE.replace("Revoking it", "Deleting it"),
        "api_key_id": PLAIN, "organization_count": 2, "organization_slugs": ["acme-cloud", "fossgo"],
        "hidden_organization_count": 0, "remedies": ["transfer_owner", "delete_organization", "rotate_secret"],
    }
    nothing_changed(owned)
    record = denial(owned)
    assert (record["action"], record["target_id"]) == (audited, PLAIN)
    assert record["details"]["owned_organization_slugs"] == ["acme-cloud", "fossgo"]
    assert record["details"]["owned_organization_count"] == 2
    assert record["details"]["owned_organization_hidden_count"] == 0
    assert owned["locks"] == [PLAIN]


@pytest.mark.parametrize("method, suffix", [("POST", "/revoke"), ("DELETE", "")], ids=["revoke", "delete"])
async def test_a_partner_sees_only_the_organizations_it_belongs_to(
    owned: Dict[str, Any], method: str, suffix: str
) -> None:
    owned["scope"](PARTNER)
    response = await send(PARTNER, owned["db"], method, f"{KEYS}/{PLAIN}{suffix}")
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert (detail["organization_count"], detail["organization_slugs"], detail["hidden_organization_count"]) == (
        2, ["acme-cloud"], 1,
    )
    assert "fossgo" not in response.text, "a partner was told the slug of an organization it cannot see"
    assert "(acme-cloud and 1 you cannot see)" in detail["message"]
    assert "Ask an administrator to transfer them" in detail["message"]
    nothing_changed(owned)
    # note: the record names what the answer named. The caller who triggered it can read it.
    record = denial(owned)
    assert (record["details"]["owned_organization_slugs"], record["details"]["owned_organization_count"],
            record["details"]["owned_organization_hidden_count"]) == (["acme-cloud"], 2, 1)
    assert "fossgo" not in repr(record)


async def test_one_organization_reads_in_the_singular(owned: Dict[str, Any]) -> None:
    owned["db"].sync.query(Organization).filter(Organization.slug == "fossgo").delete()
    owned["db"].sync.commit()
    owned["scope"](ADMIN)
    detail = (await send(ADMIN, owned["db"], "POST", f"{KEYS}/{PLAIN}/revoke")).json()["detail"]
    assert detail["message"].startswith(
        "This key owns 1 OrcaHub organization (acme-cloud). Revoking it would leave it with an owner"
    )
    assert "so its sync, member updates and provisioning would stop. Transfer it to another" in detail["message"]


# note: what does not count

@pytest.mark.parametrize("method, suffix", [("POST", "/revoke"), ("DELETE", "")], ids=["revoke", "delete"])
async def test_a_key_that_owns_nothing_is_ended_as_before(owned: Dict[str, Any], method: str, suffix: str) -> None:
    owned["db"].sync.query(Organization).delete()
    owned["db"].sync.commit()
    owned["scope"](PARTNER)
    response = await send(PARTNER, owned["db"], method, f"{KEYS}/{PLAIN}{suffix}")
    assert response.status_code == 200, response.text
    assert owned["evicted"] == [PLAIN]
    assert [c["result"] for c in owned["log"].calls] == ["success"]


async def test_inactive_and_dashboard_organizations_do_not_count(owned: Dict[str, Any]) -> None:
    db = owned["db"]
    db.sync.query(Organization).delete()
    db.sync.commit()
    own(db, PLAIN, "retired", active=False)
    own(db, PLAIN, "local-org", source="local")
    owned["scope"](ADMIN)
    assert (await send(ADMIN, db, "POST", f"{KEYS}/{PLAIN}/revoke")).status_code == 200


# note: the order of the checks

@pytest.mark.parametrize("method, suffix", [("POST", "/revoke"), ("DELETE", "")], ids=["revoke", "delete"])
async def test_an_id_outside_the_callers_clusters_is_404_before_any_409(
    owned: Dict[str, Any], method: str, suffix: str
) -> None:
    owned["scope"](PARTNER, {"nuc2"})
    response = await send(PARTNER, owned["db"], method, f"{KEYS}/{PLAIN}{suffix}")
    assert (response.status_code, response.json()["detail"]) == (404, "API key not found")
    nothing_changed(owned)
    assert owned["log"].calls == []


@pytest.mark.parametrize("method, suffix", [("POST", "/revoke"), ("DELETE", "")], ids=["revoke", "delete"])
async def test_ownership_that_cannot_be_read_ends_nothing(owned: Dict[str, Any], method: str, suffix: str) -> None:
    owned["scope"](ADMIN)
    response = await send(ADMIN, _Down(), method, f"{KEYS}/{PLAIN}{suffix}")
    assert (response.status_code, response.json()["detail"]) == (503, OWNERSHIP_UNKNOWN_DETAIL)
    assert "postgresql" not in response.text and "pw@" not in response.text
    nothing_changed(owned)


# note: withdrawing org.sync or inventory.read freezes the organizations as surely as a revoke.
# /sync admits a key holding inventory.read, and its organization half needs org.sync too.

def store(owned: Dict[str, Any], key_id: str, metadata: Dict[str, Any]) -> None:
    owned["vault"].store[_FakeVault._path(key_id, "metadata")] = metadata


@pytest.mark.parametrize("before", [["inventory.read", "org.sync"], None], ids=["scoped", "legacy"])
async def test_withdrawing_org_sync_from_an_owner_is_refused(owned: Dict[str, Any], before: Optional[List[str]]) -> None:
    metadata = plain_metadata()
    if before is None:
        metadata.pop("capabilities")
    else:
        metadata["capabilities"] = before
    store(owned, PLAIN, metadata)
    owned["scope"](PARTNER)
    response = await send(PARTNER, owned["db"], "PUT", f"{KEYS}/{PLAIN}/capabilities",
                          {"capabilities": ["inventory.read", "tenant.provision"]})
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert (detail["error"], detail["action"]) == ("key_owns_organizations", "capabilities")
    assert "Withdrawing its org.sync capability would leave them with an owner that can no longer sync them" in (
        detail["message"]
    )
    assert detail["remedies"] == ["transfer_owner", "delete_organization"]
    assert owned["vault"].writes == [] and owned["evicted"] == []
    assert denial(owned)["action"] == "integration.api_key.capabilities.update"


@pytest.mark.parametrize("after, named", [
    (["org.sync", "tenant.provision"], "Withdrawing its inventory.read capability would leave them"),
    (["tenant.provision"], "Withdrawing its inventory.read and org.sync capabilities would leave them"),
], ids=["inventory_read", "both"])
async def test_withdrawing_inventory_read_from_an_owner_is_refused(
    owned: Dict[str, Any], after: List[str], named: str
) -> None:
    store(owned, PLAIN, {**plain_metadata(), "capabilities": ["inventory.read", "org.sync"]})
    owned["scope"](ADMIN)
    response = await send(ADMIN, owned["db"], "PUT", f"{KEYS}/{PLAIN}/capabilities", {"capabilities": after})
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert (detail["error"], detail["action"]) == ("key_owns_organizations", "capabilities")
    assert named in detail["message"]
    assert owned["vault"].writes == [] and owned["evicted"] == []
    assert denial(owned)["action"] == "integration.api_key.capabilities.update"


@pytest.mark.parametrize("after", [["inventory.read", "org.sync"], ["inventory.read", "org.sync", "tenant.provision"]])
async def test_an_owner_keeps_any_edit_that_keeps_what_it_syncs_with(owned: Dict[str, Any], after: List[str]) -> None:
    store(owned, PLAIN, {**plain_metadata(), "capabilities": ["inventory.read", "org.sync"]})
    owned["scope"](PARTNER)
    response = await send(PARTNER, owned["db"], "PUT", f"{KEYS}/{PLAIN}/capabilities", {"capabilities": after})
    assert response.status_code == 200, response.text
    assert owned["evicted"] == [PLAIN]


async def test_a_key_that_owns_nothing_may_lose_org_sync(owned: Dict[str, Any]) -> None:
    owned["db"].sync.query(Organization).delete()
    owned["db"].sync.commit()
    store(owned, PLAIN, {**plain_metadata(), "capabilities": ["inventory.read", "org.sync"]})
    owned["scope"](PARTNER)
    response = await send(PARTNER, owned["db"], "PUT", f"{KEYS}/{PLAIN}/capabilities",
                          {"capabilities": ["inventory.read"]})
    assert response.status_code == 200, response.text


# note: an add-on credential that owns organizations

async def test_an_add_on_owner_is_told_to_suspend_the_add_on(owned: Dict[str, Any]) -> None:
    own(owned["db"], ADDON, "addon-org")
    owned["scope"](ADMIN)
    for method, suffix in (("POST", "/revoke"), ("DELETE", "")):
        response = await send(ADMIN, owned["db"], method, f"{KEYS}/{ADDON}{suffix}")
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert detail["error"] == "key_owns_organizations", "the installation claim answered before ownership"
        assert detail["message"].endswith("Suspend the add-on to stop it now.")
        assert "rotate the secret" not in detail["message"]
        assert detail["remedies"] == ["transfer_owner", "delete_organization", "suspend_add_on"]
    nothing_changed(owned, ADDON)
    stored = owned["vault"].store[_FakeVault._path(ADDON, "metadata")]
    assert (stored["is_revoked"], stored["extension_install_id"]) == (False, addon_metadata()["extension_install_id"])


# note: the key list and detail say what each key owns

async def test_the_key_list_names_what_each_key_owns(owned: Dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    docs = [plain_metadata(), addon_metadata()]
    monkeypatch.setattr(integrations, "list_api_keys_from_vault", lambda _v: [dict(d) for d in docs])
    for user, slugs, hidden in ((ADMIN, ["acme-cloud", "fossgo"], 0), (PARTNER, ["acme-cloud"], 1)):
        owned["scope"](user)
        response = await send(user, owned["db"], "GET", f"{KEYS}?fresh=true")
        assert response.status_code == 200, response.text
        rows = {row["id"]: row for row in response.json()["keys"]}
        plain = rows[PLAIN]
        assert (plain["owned_organization_count"], plain["owned_organization_slugs"],
                plain["owned_organization_hidden_count"]) == (2, slugs, hidden)
        assert (rows[ADDON]["owned_organization_count"], rows[ADDON]["is_add_on_credential"]) == (0, True)
        detail = (await send(user, owned["db"], "GET", f"{KEYS}/{PLAIN}")).json()
        assert (detail["owned_organization_count"], detail["owned_organization_slugs"]) == (2, slugs)


async def test_a_list_whose_ownership_cannot_be_read_still_renders(
    owned: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(integrations, "list_api_keys_from_vault", lambda _v: [plain_metadata()])
    owned["scope"](ADMIN)
    response = await send(ADMIN, _Down(), "GET", f"{KEYS}?fresh=true")
    assert response.status_code == 200, response.text
    (row,) = response.json()["keys"]
    assert (row["owned_organization_count"], row["owned_organization_slugs"]) == (None, [])
    assert ALIAS_ADMIN.email not in response.text
