"""A LastOwner raised while an owner key ends answers 409 last_owner, before any Vault write, never a 500.

The refusal that runs first decides on the same locked rows, so organization_owners.remove_owner (or
drop_unable_primary) finding no other owner is not expected. If it ever does, every route that ends a
key answers the fixed 409 and writes nothing: revoke, delete, the capability edit, uninstall, the
Administrator's removal and an owner key's own removal.
"""

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

from app.models.db_models import Organization
from app.routers import extension_installs, integrations
from app.services import integration_key_ownership
from app.services.integration_key_ownership import LAST_OWNER_DETAIL
from app.services.organization_owners import LastOwner, NoAbleOwner, add_owner, owner_key_ids
from tests.test_a_capability_grant_leaves_a_record import _FakeVault
from tests.test_a_co_owned_organization_survives_its_owner_ending import (  # noqa: F401
    CO,
    ORGS,
    able_metadata,
    co_own,
    co_owned,
    primary,
)
from tests.test_an_add_on_credential_keeps_its_organizations import ADMIN as ADDON_ADMIN
from tests.test_an_add_on_credential_keeps_its_organizations import OLD, install, own, teardown, world_db  # noqa: F401
from tests.test_an_add_on_credential_refuses_grant_edits import PLAIN, surface  # noqa: F401
from tests.test_an_owner_key_vouches_for_a_key_it_holds import EXT, OWNER, OWNER_KEYS, call, enroll, world  # noqa: F401
from tests.test_the_add_on_key_rules_hold_through_the_key_routes import ADMIN, KEYS, keys, send  # noqa: F401


def _raise(error: type) -> Any:
    async def late(*_args: Any, **_kwargs: Any) -> None:
        raise error()

    return late


@pytest.mark.parametrize("method, suffix, body, target, error", [
    ("POST", "/revoke", None, "remove_owner", LastOwner),
    ("DELETE", "", None, "remove_owner", LastOwner),
    ("PUT", "/capabilities", {"capabilities": ["inventory.read", "tenant.provision"]}, "drop_unable_primary", NoAbleOwner),
], ids=["revoke", "delete", "capabilities"])
async def test_ending_a_key_answers_409_and_writes_nothing(
    co_owned: Dict[str, Any], monkeypatch: pytest.MonkeyPatch,  # noqa: F811
    method: str, suffix: str, body: Any, target: str, error: type,
) -> None:
    db = co_owned["db"]
    for org_id in (co_owned["acme"], co_owned["globex"]):
        await co_own(db, org_id, CO)
    monkeypatch.setattr(integration_key_ownership, target, _raise(error))
    response = await send(ADMIN, db, method, f"{KEYS}/{PLAIN}{suffix}", body)
    assert (response.status_code, response.json()["detail"]) == (409, LAST_OWNER_DETAIL)
    assert co_owned["vault"].writes == [] and co_owned["vault"].deletes == [] and co_owned["evicted"] == []
    assert await owner_key_ids(db, co_owned["acme"]) == [PLAIN, CO] and primary(db, co_owned["acme"]) == PLAIN


async def test_the_administrators_removal_answers_409(
    co_owned: Dict[str, Any], monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    monkeypatch.setattr(integrations, "multi_owner_enabled", lambda: True)
    await co_own(co_owned["db"], co_owned["acme"], CO)
    monkeypatch.setattr(integrations, "remove_owner", _raise(LastOwner))
    response = await send(ADMIN, co_owned["db"], "DELETE", f"{ORGS}/{co_owned['acme']}/owner-keys/{PLAIN}?reason=r")
    assert response.status_code == 409 and response.json()["detail"]["error"] == "last_owner"


async def test_an_owner_keys_own_removal_answers_409(world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    assert (await call(world, OWNER, "POST", OWNER_KEYS, enroll())).json()["added"] is True
    monkeypatch.setattr(integrations, "remove_owner", _raise(LastOwner))
    response = await call(world, OWNER, "DELETE", f"/api/v1/integrations/external/organizations/{EXT}/owner-keys/self")
    assert response.status_code == 409 and response.json()["detail"]["error"] == "last_owner"


async def test_an_uninstall_answers_409_and_tears_nothing_down(
    teardown: List[str], monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    db = world_db()
    own(db, OLD, "addon-org")
    org = db.sync.query(Organization).one()
    assert await add_owner(db, org, CO, via="admin", by="admin@example.test")
    await db.commit()
    vault = _FakeVault(able_metadata(CO))
    monkeypatch.setattr(integration_key_ownership, "remove_owner", _raise(LastOwner))

    async def _load(_db: Any, _install_id: int, _user: Any) -> tuple:
        return install(), SimpleNamespace(slug="backup-bot"), "acme"

    monkeypatch.setattr(extension_installs, "_load", _load)
    monkeypatch.setattr(extension_installs, "get_vault_integrations_client", lambda: vault)
    with pytest.raises(HTTPException) as refused:
        await extension_installs.uninstall(7, None, ADDON_ADMIN, db)
    assert (refused.value.status_code, refused.value.detail) == (409, LAST_OWNER_DETAIL)
    assert teardown == []
    assert await owner_key_ids(db, org.id) == [OLD, CO]
