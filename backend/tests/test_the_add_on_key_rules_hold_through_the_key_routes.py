"""The add-on credential rules, sent through the real /api/v1/integrations/api-keys routes.

A key carrying extension_install_id refuses capability and cluster permission edits (409, before
any cluster is read) for every caller, an Admin included. DELETE is refused only while an
installation claims the key as api_key_id or previous_api_key_id, and a claim that cannot be read
deletes nothing (503). Revoke and the reads stay. The real RBAC dependencies run, so a tenant is
403 with its denial record, and an id outside the caller's clusters is 404 before any 409.

Every case asserts the status, the detail, the audit record, the Vault writes and deletes, the
validation cache eviction, and whether the cluster read ran.
"""

import json
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx
import pytest
from fastapi import FastAPI

from app.core import dependencies, rbac
from app.core.auth import AuthenticatedUser, get_current_user
from app.core.database import get_db
from app.core.org_scope import org_scope_dependency
from app.routers import integrations
from app.services.add_on_credential_edits import CLAIM_UNKNOWN_DETAIL, DELETE_DETAIL, GRANT_EDIT_DETAIL
from app.services.integration_key_ownership import OWNERSHIP_UNKNOWN_DETAIL
from app.services.lxd_service import get_lxd_service
from tests._sqlite_session import org_tables, sqlite_session
from tests.test_a_capability_grant_leaves_a_record import PARTNER, SECRETS, _FakeVault, _RecordingLogger
from tests.test_an_add_on_credential_refuses_grant_edits import (
    permanent_deletes,
    ADDON,
    PLAIN,
    addon_metadata,
    installations,
    plain_metadata,
    surface,  # noqa: F401
)

_HTTP = httpx.AsyncClient
KEYS = "/api/v1/integrations/api-keys"
ADMIN = AuthenticatedUser(user_id="a1", email="admin@example.test", groups=["role_admin"])
ALIAS_ADMIN = AuthenticatedUser(user_id="a2", email="alias@example.test", groups=["authentik Admins"])
TENANT = AuthenticatedUser(user_id="t1", email="tenant@example.test", groups=["role_tenant"])
USERS = {"partner": PARTNER, "admin": ADMIN, "alias_admin": ALIAS_ADMIN}
EDITS = {"capabilities": ["inventory.read", "lxd.proxy", "org.sync"],
         "permissions": [{"cluster_id": "nuc1", "permissions": ["read"]}]}


def keys_app(user: AuthenticatedUser, db: Any) -> FastAPI:
    """The key router with its real RBAC and read-only dependencies. The login and the db are faked."""
    app = FastAPI()
    app.include_router(integrations.router, prefix="/api/v1")

    async def _user() -> AuthenticatedUser:
        return user

    async def _db() -> AsyncIterator[Any]:
        yield db

    async def _none() -> None:
        return None

    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[org_scope_dependency] = _none
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_lxd_service] = _none
    return app


async def send(user: AuthenticatedUser, db: Any, method: str, path: str, body: Any = None) -> httpx.Response:
    content = None if body is None and method in ("GET", "DELETE", "POST") else json.dumps(body).encode()
    app = keys_app(user, db)
    async with _HTTP(transport=httpx.ASGITransport(app=app), base_url="http://orcastra.test") as client:
        return await client.request(method, path, content=content, headers={"content-type": "application/json"})


@pytest.fixture
def keys(surface: Dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:  # noqa: F811
    """surface plus both keys in one Vault, the RBAC denial record, and no cached roles."""
    vault = surface["use"](addon_metadata())
    vault.store[_FakeVault._path(PLAIN, "metadata")] = plain_metadata()
    vault.store[_FakeVault._path(PLAIN, "secret")] = {"hash": SECRETS[1], "raw": SECRETS[0]}
    surface["denials"] = _RecordingLogger()
    monkeypatch.setattr(rbac, "activity_logger", surface["denials"])

    def scope(user: AuthenticatedUser, clusters: Optional[set] = None) -> None:
        allowed = None if user in (ADMIN, ALIAS_ADMIN) else (clusters or {"nuc1"})
        monkeypatch.setattr(integrations, "get_current_allowed_clusters", lambda: allowed)

    surface["scope"] = scope
    return surface


def unchanged(keys: Dict[str, Any], *, deletes: bool = True) -> None:
    vault = keys["vault"]
    assert vault.writes == [] and keys["log"].calls == [] and keys["evicted"] == []
    assert keys["reached"] == [] and keys["cache"].cleared == []
    if deletes:
        assert vault.deletes == []
    assert vault.store[_FakeVault._path(ADDON, "metadata")]["capabilities"] == ["inventory.read", "lxd.proxy"]


# Grant edits.

@pytest.mark.parametrize("who", list(USERS))
@pytest.mark.parametrize("route, payload", [
    ("capabilities", {"capabilities": ["inventory.read", "lxd.proxy", "org.sync"]}),
    ("capabilities", {"capabilities": ["inventory.read"]}),
    ("capabilities", {"capabilities": ["inventory.read", "lxd.proxy"]}),
    ("permissions", [{"cluster_id": "nuc1", "permissions": ["read"]}]),
    ("permissions", [{"cluster_id": "nuc1", "permissions": ["admin"]},
                     {"cluster_id": "nuc2", "permissions": ["read"]}]),
    ("permissions", []),
], ids=["caps_widen", "caps_narrow", "caps_same", "perms_same", "perms_widen", "perms_empty"])
async def test_a_grant_edit_of_an_add_on_credential_is_409_for_every_caller(
    keys: Dict[str, Any], who: str, route: str, payload: Any,
) -> None:
    user = USERS[who]
    keys["scope"](user)
    response = await send(user, installations((ADDON, None)), "PUT", f"{KEYS}/{ADDON}/{route}", payload)
    assert (response.status_code, response.json()["detail"]) == (409, GRANT_EDIT_DETAIL)
    unchanged(keys)


INSTALL_IDS = [7, "7", 0, "", False, [7], {"id": 7}]


@pytest.mark.parametrize("install_id", [*INSTALL_IDS, None], ids=[repr(v) for v in [*INSTALL_IDS, None]])
@pytest.mark.parametrize("route", ["capabilities", "permissions"])
async def test_any_non_null_install_id_makes_it_an_add_on_credential(
    keys: Dict[str, Any], install_id: Any, route: str,
) -> None:
    keys["vault"].store[_FakeVault._path(ADDON, "metadata")]["extension_install_id"] = install_id
    keys["scope"](PARTNER)
    response = await send(PARTNER, installations(), "PUT", f"{KEYS}/{ADDON}/{route}",
                          {"capabilities": EDITS["capabilities"]} if route == "capabilities" else EDITS[route])
    if install_id is not None:
        assert (response.status_code, response.json()["detail"]) == (409, GRANT_EDIT_DETAIL)
        assert keys["vault"].writes == [] and keys["reached"] == []
        return
    assert response.status_code == 200, response.text
    (path, written), = keys["vault"].writes
    assert path == _FakeVault._path(ADDON, "metadata") and keys["evicted"] == [ADDON]
    if route == "capabilities":
        assert written["capabilities"] == sorted(EDITS["capabilities"])
        assert [c["action"] for c in keys["log"].calls] == ["integration.api_key.capabilities.update"]
    else:
        assert [(p["cluster_id"], p["permissions"]) for p in written["cluster_permissions"]] == [("nuc1", ["read"])]
        assert keys["reached"]


@pytest.mark.parametrize("route", ["capabilities", "permissions"])
async def test_an_ordinary_key_edit_still_writes(keys: Dict[str, Any], route: str) -> None:
    keys["scope"](PARTNER)
    payload = {"capabilities": EDITS["capabilities"]} if route == "capabilities" else EDITS[route]
    response = await send(PARTNER, installations(), "PUT", f"{KEYS}/{PLAIN}/{route}", payload)
    assert response.status_code == 200, response.text
    assert [p for p, _ in keys["vault"].writes] == [_FakeVault._path(PLAIN, "metadata")]
    assert keys["evicted"] == [PLAIN]


BAD_BODIES = [True, "inventory.read", 1, None, [], [1], [None], {"x": 1}]


@pytest.mark.parametrize("route", ["capabilities", "permissions"])
@pytest.mark.parametrize("value", BAD_BODIES, ids=[repr(v) for v in BAD_BODIES])
@pytest.mark.parametrize("key_id", [ADDON, PLAIN])
async def test_a_malformed_edit_body_is_422_and_writes_nothing(
    keys: Dict[str, Any], route: str, value: Any, key_id: str,
) -> None:
    keys["scope"](PARTNER)
    payload = {"capabilities": value} if route == "capabilities" else value
    response = await send(PARTNER, installations(), "PUT", f"{KEYS}/{key_id}/{route}", payload)
    if route == "permissions" and value == []:
        assert response.status_code == (409 if key_id == ADDON else 200)
        return
    assert response.status_code == 422
    assert keys["vault"].writes == [] and keys["reached"] == [] and keys["log"].calls == []


# Delete.

class _Down:
    statements: List[Any] = []

    async def execute(self, *_a: Any, **_k: Any) -> Any:
        raise ConnectionError("database is down: postgresql://user:pw@db/app")


# claim_unreadable holds the organization tables and no installations table, so the ownership
# read answers (nothing owned) and the claim read is the one that fails. ownership_unreadable fails
# the first read, which is the ownership check.
DELETES = [
    ("claimed_current", lambda: installations(("oak_other", None), (ADDON, None)), 409, DELETE_DETAIL),
    ("claimed_previous", lambda: installations(("oak_next", ADDON)), 409, DELETE_DETAIL),
    ("unclaimed", lambda: installations(("oak_next", "oak_between")), 200, None),
    ("no_installation_at_all", lambda: installations(), 200, None),
    ("claim_unreadable", lambda: sqlite_session(*org_tables()), 503, CLAIM_UNKNOWN_DETAIL),
    ("ownership_unreadable", lambda: _Down(), 503, OWNERSHIP_UNKNOWN_DETAIL),
]


@pytest.mark.parametrize("who", list(USERS))
@pytest.mark.parametrize("db_of, status, detail", [c[1:] for c in DELETES], ids=[c[0] for c in DELETES])
async def test_deleting_an_add_on_credential_turns_on_the_claim(
    keys: Dict[str, Any], who: str, db_of: Any, status: int, detail: Optional[str],
) -> None:
    user = USERS[who]
    keys["scope"](user)
    response = await send(user, db_of(), "DELETE", f"{KEYS}/{ADDON}")
    assert response.status_code == status
    if detail is not None:
        assert response.json()["detail"] == detail and "postgresql" not in response.text
        unchanged(keys)
        return
    assert response.json() == {"success": True, "message": f"API key {ADDON} deleted"}
    assert keys["vault"].deletes == permanent_deletes(ADDON)
    assert keys["evicted"] == [ADDON]
    assert [c["action"] for c in keys["log"].calls] == ["integration.api_key.delete"]


async def test_an_ordinary_key_delete_never_reads_the_installations(keys: Dict[str, Any]) -> None:
    keys["scope"](PARTNER)
    db = installations((PLAIN, None))
    response = await send(PARTNER, db, "DELETE", f"{KEYS}/{PLAIN}")
    assert response.status_code == 200
    assert not any("extension_installations" in str(statement) for statement in db.statements)
    assert keys["vault"].deletes == permanent_deletes(PLAIN)


SPELLINGS = [(f"{KEYS}/{ADDON}", 409), (f"{KEYS}/%6Fak_addon", 409), (f"{KEYS}/{ADDON}?force=true", 409),
             (f"{KEYS}/{ADDON}/", 307), (f"{KEYS}/{ADDON.upper()}", 404), (f"{KEYS}/{ADDON}%20", 404),
             (f"{KEYS}/{PLAIN}%2F..%2F{ADDON}", 404), (f"{KEYS}//{ADDON}", 404), (f"{KEYS}/{ADDON}%3Fx", 404)]


@pytest.mark.parametrize("path, status", SPELLINGS, ids=[s[0].split("api-keys", 1)[1] for s in SPELLINGS])
async def test_delete_spellings_of_a_claimed_add_on_credential(keys: Dict[str, Any], path: str, status: int) -> None:
    keys["scope"](PARTNER)
    response = await send(PARTNER, installations((ADDON, None)), "DELETE", path)
    assert response.status_code == status
    unchanged(keys)


# Other methods, the kill switch, and who may call.

METHODS = [("GET", "", 200), ("PUT", "", 405), ("PATCH", "", 405), ("POST", "", 405),
           *[(m, "/capabilities", 405) for m in ("GET", "POST", "PATCH", "DELETE")],
           *[(m, "/permissions", 405) for m in ("GET", "POST", "PATCH", "DELETE")],
           ("GET", "/revoke", 405), ("PUT", "/revoke", 405)]


@pytest.mark.parametrize("method, suffix, status", METHODS, ids=[f"{m}{s or '/'}" for m, s, _ in METHODS])
async def test_no_other_method_edits_an_add_on_credential(
    keys: Dict[str, Any], method: str, suffix: str, status: int,
) -> None:
    keys["scope"](PARTNER)
    path = f"{KEYS}/{ADDON}{suffix}"
    response = await send(PARTNER, installations((ADDON, None)), method, path, EDITS["capabilities"])
    assert response.status_code == status
    unchanged(keys)


@pytest.mark.parametrize("who", list(USERS))
async def test_revoke_stays_the_kill_switch(keys: Dict[str, Any], who: str) -> None:
    keys["scope"](USERS[who])
    response = await send(USERS[who], installations((ADDON, None)), "POST", f"{KEYS}/{ADDON}/revoke")
    assert response.status_code == 200 and response.json()["is_revoked"] is True
    stored = keys["vault"].store[_FakeVault._path(ADDON, "metadata")]
    assert stored["is_revoked"] is True and stored["extension_install_id"] == 7
    assert keys["evicted"] == [ADDON]
    assert [c["action"] for c in keys["log"].calls] == ["integration.api_key.revoke"]


ROUTES = [("PUT", "/capabilities", {"capabilities": ["inventory.read"]}), ("PUT", "/permissions", []),
          ("DELETE", "", None), ("POST", "/revoke", None)]


@pytest.mark.parametrize("method, suffix, payload", ROUTES, ids=[f"{m}{s or '/'}" for m, s, _ in ROUTES])
async def test_a_tenant_is_403_with_its_denial_recorded(
    keys: Dict[str, Any], method: str, suffix: str, payload: Any,
) -> None:
    keys["scope"](TENANT)
    response = await send(TENANT, installations(), method, f"{KEYS}/{ADDON}{suffix}", payload)
    assert (response.status_code, response.json()["detail"]) == (403, "Insufficient permissions")
    (denial,) = keys["denials"].calls
    assert denial["action"] == "rbac.access_denied" and denial["details"]["method"] == method
    unchanged(keys)


@pytest.mark.parametrize("method, suffix, payload", ROUTES, ids=[f"{m}{s or '/'}" for m, s, _ in ROUTES])
async def test_an_id_outside_the_callers_clusters_is_404_before_any_409(
    keys: Dict[str, Any], method: str, suffix: str, payload: Any,
) -> None:
    keys["scope"](PARTNER, {"nuc2"})
    response = await send(PARTNER, installations((ADDON, None)), method, f"{KEYS}/{ADDON}{suffix}", payload)
    assert (response.status_code, response.json()["detail"]) == (404, "API key not found")
    unchanged(keys)


@pytest.mark.parametrize("method, suffix, payload", ROUTES, ids=[f"{m}{s or '/'}" for m, s, _ in ROUTES])
async def test_read_only_mode_refuses_first(
    keys: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, method: str, suffix: str, payload: Any,
) -> None:
    locked = dependencies.get_settings().model_copy(update={"read_only_mode": True})
    monkeypatch.setattr(dependencies, "get_settings", lambda: locked)
    keys["scope"](PARTNER)
    response = await send(PARTNER, installations(), method, f"{KEYS}/{ADDON}{suffix}", payload)
    assert response.status_code == 403 and "read-only" in response.json()["detail"]
    unchanged(keys)
