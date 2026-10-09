"""A LastOwner raised while an owner key ends answers 409 last_owner, before any Vault write, never a 500.

The refusal that runs first decides on the same locked rows, so organization_owners.remove_owner finding
no other owner is not expected. If it ever does, every route that ends a key answers the fixed 409 and
writes nothing: revoke, delete and the Administrator's removal. A successor that cannot be read at that
point answers the fixed 503.
"""

from typing import Any, Dict

import pytest

from app.routers import integrations
from app.services import integration_key_ownership
from app.services.integration_key_ownership import LAST_OWNER_DETAIL, OWNERSHIP_UNKNOWN_DETAIL
from app.services.organization_owners import LastOwner, NoAbleOwner, owner_key_ids
from tests.test_a_co_owned_organization_survives_its_owner_ending import (  # noqa: F401
    CO,
    ORGS,
    co_own,
    co_owned,
    primary,
)
from tests.test_a_key_that_owns_an_organization_is_not_revoked_or_deleted import (  # noqa: F401
    ADMIN,
    KEYS,
    PLAIN,
    owned,
    send,
)


def _raise(error: Any) -> Any:
    async def late(*_args: Any, **_kwargs: Any) -> None:
        raise error

    return late


def _untouched(world: Dict[str, Any]) -> None:
    assert world["vault"].writes == [] and world["vault"].deletes == [] and world["evicted"] == []
    assert world["owner_log"].calls == []


@pytest.mark.parametrize("method, suffix", [("POST", "/revoke"), ("DELETE", "")], ids=["revoke", "delete"])
@pytest.mark.parametrize("error", [LastOwner(), NoAbleOwner()], ids=["last_owner", "no_able_owner"])
async def test_ending_a_key_answers_409_and_writes_nothing(
    co_owned: Dict[str, Any], monkeypatch: pytest.MonkeyPatch,  # noqa: F811
    method: str, suffix: str, error: Exception,
) -> None:
    db = co_owned["db"]
    for org_id in (co_owned["acme"], co_owned["nimbus"]):
        await co_own(db, org_id, CO)
    monkeypatch.setattr(integration_key_ownership, "remove_owner", _raise(error))
    response = await send(co_owned, ADMIN, method, f"{KEYS}/{PLAIN}{suffix}")
    assert (response.status_code, response.json()["detail"]) == (409, LAST_OWNER_DETAIL)
    _untouched(co_owned)
    assert await owner_key_ids(db, co_owned["acme"]) == [PLAIN, CO] and primary(db, co_owned["acme"]) == PLAIN


@pytest.mark.parametrize("method, suffix", [("POST", "/revoke"), ("DELETE", "")], ids=["revoke", "delete"])
async def test_an_unreadable_successor_while_ending_is_503(
    co_owned: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, method: str, suffix: str  # noqa: F811
) -> None:
    db = co_owned["db"]
    for org_id in (co_owned["acme"], co_owned["nimbus"]):
        await co_own(db, org_id, CO)
    monkeypatch.setattr(integration_key_ownership, "remove_owner", _raise(LookupError(CO)))
    response = await send(co_owned, ADMIN, method, f"{KEYS}/{PLAIN}{suffix}")
    assert (response.status_code, response.json()["detail"]) == (503, OWNERSHIP_UNKNOWN_DETAIL)
    _untouched(co_owned)


async def test_the_administrators_removal_answers_409(
    co_owned: Dict[str, Any], monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    monkeypatch.setattr(integrations, "multi_owner_enabled", lambda: True)
    await co_own(co_owned["db"], co_owned["acme"], CO)
    monkeypatch.setattr(integrations, "remove_owner", _raise(LastOwner()))
    response = await send(co_owned, ADMIN, "DELETE", f"{ORGS}/{co_owned['acme']}/owner-keys/{PLAIN}?reason=r")
    assert response.status_code == 409 and response.json()["detail"]["error"] == "last_owner"
    _untouched(co_owned)
    assert await owner_key_ids(co_owned["db"], co_owned["acme"]) == [PLAIN, CO]
