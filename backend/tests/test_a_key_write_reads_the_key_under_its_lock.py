"""A key write reads the key under the key's lock, so it never writes back over a revoke.

Revoke, the capability and permission edits and a secret rotation each read the key's stored
record, change it and write it back. Revoke and the capability edit used to read it before taking
the key's ownership lock, and the permission edit and rotation took no lock at all, so a revoke
that landed between one of those reads and its write was written over and the key worked again.
Each now reads the record inside the lock, where a write that held the lock first is already
in Vault.

The seam below lands another write at the moment a handler is granted the lock, which is the
latest point a read made before the lock would have missed.
"""

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict

import pytest

from app.routers import integrations
from tests.test_a_capability_grant_leaves_a_record import _FakeVault
from tests.test_an_add_on_credential_refuses_grant_edits import (
    PLAIN,
    installations,
    surface,  # noqa: F401
)
from tests.test_the_add_on_key_rules_hold_through_the_key_routes import (
    ADMIN,
    KEYS,
    keys,  # noqa: F401
    send,
)

REVOKED = {"is_revoked": True, "is_active": False, "revoked_at": "2026-10-08T09:00:00"}


@pytest.fixture
def lands(keys: Dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:  # noqa: F811
    """keys, plus `landed`: changes another writer makes to PLAIN just before the lock is granted."""
    real_lock = integrations.serialize_key_ownership
    keys["landed"] = {}

    @asynccontextmanager
    async def _another_write_first(session: Any, key_id: str) -> AsyncIterator[None]:
        async with real_lock(session, key_id):
            path = _FakeVault._path(key_id, "metadata")
            keys["vault"].store[path] = {**keys["vault"].store[path], **keys["landed"]}
            yield

    monkeypatch.setattr(integrations, "serialize_key_ownership", _another_write_first)
    keys["scope"](ADMIN)
    return keys


def stored(lands: Dict[str, Any]) -> Dict[str, Any]:
    return lands["vault"].store[_FakeVault._path(PLAIN, "metadata")]


@pytest.mark.parametrize("route, body", [
    ("capabilities", {"capabilities": ["inventory.read", "lxd.proxy"]}),
    ("permissions", [{"cluster_id": "nuc1", "permissions": ["read"]}]),
], ids=["capabilities", "permissions"])
async def test_a_grant_edit_keeps_a_revoke_that_landed_first(lands: Dict[str, Any], route: str, body: Any) -> None:
    lands["landed"].update(REVOKED)
    response = await send(ADMIN, installations(), "PUT", f"{KEYS}/{PLAIN}/{route}", body)
    assert response.status_code == 200, response.text
    assert lands["vault"].writes, "precondition: the edit wrote the key"
    assert (stored(lands)["is_revoked"], stored(lands)["is_active"]) == (True, False), "the revoke was undone"


async def test_a_revoke_keeps_an_edit_that_landed_first(lands: Dict[str, Any]) -> None:
    lands["landed"]["capabilities"] = ["inventory.read", "lxd.proxy"]
    response = await send(ADMIN, installations(), "POST", f"{KEYS}/{PLAIN}/revoke")
    assert response.status_code == 200, response.text
    assert stored(lands)["is_revoked"] is True
    assert stored(lands)["capabilities"] == ["inventory.read", "lxd.proxy"], "the capability edit was undone"


async def test_a_rotation_refuses_a_key_revoked_while_it_waited(lands: Dict[str, Any]) -> None:
    secret_before = dict(lands["vault"].store[_FakeVault._path(PLAIN, "secret")])
    lands["landed"].update(REVOKED)
    response = await send(ADMIN, installations(), "POST", f"{KEYS}/{PLAIN}/rotate-secret")
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == integrations._ROTATE_A_DEAD_KEY
    assert lands["vault"].writes == [], "a rotation wrote to a key revoked before it held the lock"
    assert lands["vault"].store[_FakeVault._path(PLAIN, "secret")] == secret_before
    assert stored(lands)["is_revoked"] is True
