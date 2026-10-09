"""A key's callback URL is used only with the secret that stored it.

A /sync stores `app_url`, and the member push, the removal push and the lifecycle webhook send the
key's raw secret there while the organization refresh sends its hash. A rotation kept the stored
URL, so a URL planted with a leaked secret received the very secret issued to replace it, and a
sync that authenticated just before a rotation could still write its URL after it.

Now a rotation clears the URL first. The sync writes the URL under the key's lock, read from Vault
itself, and only while the secret it presented is still the key's secret and the key is active.
The URL is stored with a tag of that secret, and every callback uses it only while the tag matches
the current secret. These run the real sync, rotation, refresh and callback credential code
against an in-memory Vault with check-and-set.
"""

import ast
import asyncio
import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

import pytest
from fastapi import HTTPException

from app.core.auth import AuthenticatedUser
from app.core.safe_http import GuardedResponse
from app.core.vault_client import VaultCasMismatch
from app.models.db_models import Organization
from app.routers import integrations
from app.routers.integrations import SyncDataRequest
from app.services import orcahub_sync
from app.services.integration_callback_url import APP_URL_SECRET_TAG, callback_url_tag
from tests._sqlite_session import org_tables, sqlite_session

APP = Path(__file__).resolve().parents[1] / "app"
KEY = "oak_" + "b" * 32
BASE = f"secret/data/{integrations.VAULT_INTEGRATIONS_PATH}/api_keys/{KEY}"
ADMIN = AuthenticatedUser(user_id="a", email="admin@example.test", groups=["role_admin"])
ATTACKER = "https://collector.example.test"
HUB = "https://hub.example.test"


class _Vault:
    def __init__(self, docs: Dict[str, Dict[str, Any]]) -> None:
        self.docs = docs
        self.versions: Dict[str, int] = {path: 1 for path in docs}
        self.writes: List[str] = []
        self.fail_metadata_writes = False

    def vault_read(self, path: str, use_cache: bool = True) -> Dict[str, Any]:
        if path not in self.docs:
            raise HTTPException(status_code=404, detail="not found")
        return dict(self.docs[path])

    def vault_read_versioned(self, path: str) -> tuple:
        return (dict(self.docs[path]) if path in self.docs else None), self.versions.get(path, 0)

    def vault_kv_put(self, path: str, data: Dict[str, Any], cas: Any = None) -> None:
        if self.fail_metadata_writes and path.endswith("/metadata"):
            raise integrations.VaultClientError("Vault KV put failed: 503")
        if cas is not None and cas != self.versions.get(path, 0):
            raise VaultCasMismatch(path)
        self.writes.append(path)
        self.docs[path] = dict(data)
        self.versions[path] = self.versions.get(path, 0) + 1


class _Cache:
    def __init__(self) -> None:
        self.store: Dict[tuple, Any] = {}

    def get(self, ns: str, key: str) -> Any:
        return self.store.get((ns, key))

    def set(self, ns: str, key: str, value: Any, ttl: int = 0) -> None:
        self.store[(ns, key)] = value

    def delete(self, *_a: Any, **_k: Any) -> None:
        return None

    def delete_pattern(self, *_a: Any, **_k: Any) -> None:
        return None


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    monkeypatch.setenv("SECRET_KEY", "unit-test-secret")
    old_secret, old_hash = integrations.generate_api_key_secret()
    metadata = {"id": KEY, "name": "conn", "app_name": "hub", "app_type": "orcahub", "is_active": True,
                "is_revoked": False, "created_at": "2026-09-01T00:00:00", "created_by": ADMIN.email,
                "cluster_permissions": [{"cluster_id": "c1", "permissions": ["read"], "projects": ["*"]}]}
    vault = _Vault({f"{BASE}/metadata": metadata, f"{BASE}/secret": {"hash": old_hash, "raw": old_secret}})
    events: List[Dict[str, Any]] = []

    async def authorize(_key: str, _user: Any, _db: Any) -> Dict[str, Any]:
        return vault.vault_read(f"{BASE}/metadata")

    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: vault)
    monkeypatch.setattr(integrations, "get_cache_manager", lambda: _Cache())
    monkeypatch.setattr(integrations, "_authorize_api_key", authorize)
    monkeypatch.setattr(integrations, "get_user_mappings_from_vault", lambda _v, _k: [])
    monkeypatch.setattr(integrations, "_schedule_last_used_update", lambda _k: None)
    monkeypatch.setattr(integrations.activity_logger, "log_activity", lambda **kw: events.append(kw) or "id")
    integrations._key_cache.clear()
    yield {"old": old_secret, "vault": vault, "events": events}
    integrations._key_cache.clear()


@pytest.fixture
def outbound(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    """Every request the refresh makes, answered with no organizations."""
    sent: List[Dict[str, Any]] = []

    async def recorder(method: str, url: str, **kwargs: Any) -> GuardedResponse:
        sent.append({"method": method, "url": url, **kwargs})
        return GuardedResponse(200, "application/json", json.dumps({"organizations": []}).encode())

    monkeypatch.setattr(integrations, "guarded_request", recorder)
    return sent


def _sync_body(app_url: str) -> SyncDataRequest:
    return SyncDataRequest.model_construct(
        app_url=app_url, organizations=[], members=[], include_clusters=False,
        include_projects=False, include_instances=False, cluster_ids=None,
    )


async def _sync(validated: Dict[str, Any], app_url: str, secret: Optional[str]) -> None:
    """The rest of a /sync whose dependency already validated the key with `secret`."""
    await integrations.sync_data(_sync_body(app_url), validated, None, None, secret)


async def _rotate() -> Any:
    return await integrations.rotate_api_key_secret(KEY, None, ADMIN, sqlite_session(*org_tables()))


def _org() -> Organization:
    return Organization(id=1, name="Own", slug="own", source="orcahub", external_id="ext-own", api_key_id=KEY,
                        created_by="system", is_active=True)


def _stored_url(world: Dict[str, Any]) -> Optional[str]:
    return world["vault"].docs[f"{BASE}/metadata"].get("app_url")


async def _refresh() -> Any:
    return await integrations.refresh_organizations(api_key_id=KEY, db=None, user=ADMIN)


async def _no_callback_reaches(outbound: List[Dict[str, Any]]) -> None:
    """The member push and the webhook take their URL from here, and the refresh refuses."""
    assert await orcahub_sync._get_orcahub_credentials(_org()) is None
    with pytest.raises(HTTPException) as caught:
        await _refresh()
    assert caught.value.status_code == 400
    assert outbound == []


# note: the rotation

async def test_a_rotation_clears_a_stored_url_and_records_it(world, outbound) -> None:
    old_hash = world["vault"].docs[f"{BASE}/secret"]["hash"]
    world["vault"].docs[f"{BASE}/metadata"].update(app_url=HUB, **{APP_URL_SECRET_TAG: callback_url_tag(old_hash)})
    assert (await orcahub_sync._get_orcahub_credentials(_org()))["app_url"] == HUB, "precondition: bound"
    answer = await _rotate()
    assert _stored_url(world) is None
    assert world["vault"].docs[f"{BASE}/metadata"][APP_URL_SECRET_TAG] is None
    (event,) = [e for e in world["events"] if e["action"] == integrations.API_KEY_ROTATE]
    assert event["result"] == "success" and event["details"]["callback_url_cleared"] is True
    assert HUB not in repr(event), "the audit record never names the URL"
    await _no_callback_reaches(outbound)
    assert answer.api_key_secret not in repr(world["vault"].docs[f"{BASE}/metadata"])


async def test_a_rotation_without_a_url_writes_only_the_secret(world) -> None:
    await _rotate()
    assert world["vault"].writes == [f"{BASE}/secret"]
    (event,) = [e for e in world["events"] if e["action"] == integrations.API_KEY_ROTATE]
    assert event["details"]["callback_url_cleared"] is False


async def test_a_url_that_cannot_be_cleared_stops_the_rotation(world) -> None:
    world["vault"].docs[f"{BASE}/metadata"]["app_url"] = HUB
    world["vault"].fail_metadata_writes = True
    old = dict(world["vault"].docs[f"{BASE}/secret"])
    with pytest.raises(HTTPException) as caught:
        await _rotate()
    assert caught.value.status_code == 503 and "Nothing changed" in caught.value.detail
    assert world["vault"].docs[f"{BASE}/secret"] == old, "a secret was issued that nobody was shown"


# note: the race the review found

async def test_a_sync_in_flight_across_a_rotation_cannot_plant_its_url(world, outbound) -> None:
    validated = await integrations.validate_api_key(KEY, world["old"])
    answer = await _rotate()
    await _sync(validated, ATTACKER, world["old"])
    assert _stored_url(world) != ATTACKER, "a sync authenticated with the replaced secret set the URL"
    await _no_callback_reaches(outbound)
    assert answer.api_key_secret not in repr(world["vault"].docs[f"{BASE}/metadata"])


async def test_a_url_stored_with_the_replaced_secret_is_never_used_with_the_new_one(world, outbound) -> None:
    """Even a write that slipped past the check: its tag names the secret it was stored with."""
    old_hash = world["vault"].docs[f"{BASE}/secret"]["hash"]
    await _rotate()
    world["vault"].docs[f"{BASE}/metadata"].update(app_url=ATTACKER, **{APP_URL_SECRET_TAG: callback_url_tag(old_hash)})
    await _no_callback_reaches(outbound)


async def test_a_url_stored_before_the_binding_waits_for_the_next_sync(world, outbound) -> None:
    world["vault"].docs[f"{BASE}/metadata"]["app_url"] = HUB
    await _no_callback_reaches(outbound)
    validated = await integrations.validate_api_key(KEY, world["old"])
    await _sync(validated, HUB, world["old"])
    creds = await orcahub_sync._get_orcahub_credentials(_org())
    assert creds == {"app_url": HUB, "api_key_id": KEY, "api_key_secret": world["old"]}


async def test_a_sync_with_the_current_secret_sets_the_url_every_callback_uses(
    world, outbound, monkeypatch: pytest.MonkeyPatch
) -> None:
    locks: List[str] = []
    real_lock = integrations.serialize_key_ownership

    @asynccontextmanager
    async def _seen(session: Any, key_id: str) -> AsyncIterator[None]:
        locks.append(key_id)
        async with real_lock(session, key_id):
            yield

    monkeypatch.setattr(integrations, "serialize_key_ownership", _seen)
    answer = await _rotate()
    validated = await integrations.validate_api_key(KEY, answer.api_key_secret)
    await _sync(validated, HUB, answer.api_key_secret)
    assert locks == [KEY, KEY], "the rotation and the sync's URL write each take the key's lock"
    creds = await orcahub_sync._get_orcahub_credentials(_org())
    assert creds == {"app_url": HUB, "api_key_id": KEY, "api_key_secret": answer.api_key_secret}
    await _refresh()
    (request,) = outbound
    assert request["url"] == f"{HUB}/api/external/organizations"
    assert request["headers"]["X-API-Key-Secret-Hash"] == world["vault"].docs[f"{BASE}/secret"]["hash"]


async def test_a_slow_url_write_keeps_the_key_lock_until_it_lands(world, monkeypatch: pytest.MonkeyPatch) -> None:
    """The write used to run under a timeout, outside any lock. Now the sync holds the key's lock
    until the write lands, so a revoke that wants the lock meanwhile runs after it and stands."""
    from app.core import org_scope

    vault = world["vault"]
    writing, release = threading.Event(), threading.Event()
    real_put = vault.vault_kv_put

    def slow_put(path: str, data: Dict[str, Any], cas: Any = None) -> None:
        if path == f"{BASE}/metadata" and data.get("app_url") == HUB and not release.is_set():
            writing.set()
            release.wait(5)
        real_put(path, data, cas=cas)

    monkeypatch.setattr(vault, "vault_kv_put", slow_put)
    monkeypatch.setattr(integrations, "VALIDATE_KEY_TIMEOUT", 0.1)
    validated = await integrations.validate_api_key(KEY, world["old"])
    sync = asyncio.create_task(_sync(validated, HUB, world["old"]))
    assert await asyncio.to_thread(writing.wait, 5), "precondition: the URL write is under way"

    async def revoke() -> None:
        token = org_scope._allowed_clusters_var.set(None)
        try:
            await integrations.revoke_api_key(KEY, None, ADMIN, sqlite_session(*org_tables()))
        finally:
            org_scope._allowed_clusters_var.reset(token)

    revoking = asyncio.create_task(revoke())
    await asyncio.sleep(0.5)
    assert not sync.done() and not revoking.done(), "the revoke waits for the key lock the sync holds"
    release.set()
    await asyncio.wait_for(asyncio.gather(sync, revoking), 10)
    doc = vault.docs[f"{BASE}/metadata"]
    assert doc["is_revoked"] is True and doc["app_url"] == HUB


@pytest.mark.parametrize("state", [{"is_revoked": True}, {"is_active": False}], ids=["revoked", "inactive"])
async def test_a_key_ended_while_the_sync_ran_gets_no_url(world, state: Dict[str, Any]) -> None:
    validated = await integrations.validate_api_key(KEY, world["old"])
    world["vault"].docs[f"{BASE}/metadata"].update(state)
    await _sync(validated, ATTACKER, world["old"])
    assert _stored_url(world) is None


async def test_a_sync_that_presents_no_secret_stores_no_url(world) -> None:
    validated = await integrations.validate_api_key(KEY, world["old"])
    await _sync(validated, ATTACKER, None)
    assert _stored_url(world) is None


# note: by shape, so a new reader of the stored URL cannot skip the binding

# Every function under app/ that reads a stored app_url, and why that is safe.
READERS = {
    "services/integration_callback_url.py:bound_callback_url": "the binding itself",
    "routers/integrations.py:_forget_callback_url_blocking": "presence only: whether there is a URL to clear",
    "services/orcahub_sync.py:push_members_to_orcahub": "creds from _get_orcahub_credentials, which binds",
    "services/orcahub_sync.py:_connection_home_org_id": "the creds push_members_to_orcahub hands it, which bind",
    "services/orcahub_webhook.py:_post_signed": "creds from _get_orcahub_credentials, which binds",
}


def _readers() -> Dict[str, ast.AST]:
    found: Dict[str, ast.AST] = {}
    for path in sorted(APP.rglob("*.py")):
        for fn in ast.walk(ast.parse(path.read_text())):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                by_get = (
                    isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "get" and node.args
                    and isinstance(node.args[0], ast.Constant) and node.args[0].value == "app_url"
                )
                by_key = (
                    isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load)
                    and isinstance(node.slice, ast.Constant) and node.slice.value == "app_url"
                )
                if by_get or by_key:
                    found[f"{path.relative_to(APP)}:{fn.name}"] = fn
                    break
    return found


def test_every_reader_of_a_stored_callback_url_is_reviewed() -> None:
    assert set(_readers()) == set(READERS)


def _calls(where: str) -> set:
    rel, name = where.split(":")
    (fn,) = [n for n in ast.walk(ast.parse((APP / rel).read_text()))
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
    return {getattr(n.func, "id", None) or getattr(n.func, "attr", None) for n in calls}


@pytest.mark.parametrize("where", [
    "services/orcahub_sync.py:_read_orcahub_credentials_blocking",
    "routers/integrations.py:refresh_organizations",
])
def test_the_callbacks_take_their_url_from_the_binding(where: str) -> None:
    assert "bound_callback_url" in _calls(where)


@pytest.mark.parametrize("where", [
    "services/orcahub_sync.py:push_members_to_orcahub",
    "services/orcahub_webhook.py:_emit",
])
def test_the_pushes_and_the_webhook_get_their_url_through_the_credentials(where: str) -> None:
    assert "_get_orcahub_credentials" in _calls(where)
