"""A key's callback URL is used only with the secret that stored it.

A /sync carrying org.sync stores `app_url`, and the member push, the removal push and the lifecycle
webhook send the key's raw secret there while the organization refresh sends its hash. A sync that
authenticated with a secret just before that secret was rotated could still write its URL after
the rotation had cleared it, outside any lock and without checking the secret again, and the next
member push carried the NEW secret to it.

Now the sync writes the URL under the key's lock, from Vault rather than its mirror, and only while
the secret it presented is still the key's secret and the key is active. The URL is stored with a
tag of that secret, and every callback uses it only while the tag matches the current secret. These
run the real sync, rotation, refresh and callback credential code against an in-memory Vault.
"""

import ast
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx
import pytest
from fastapi import HTTPException

from app.models.db_models import Organization
from app.routers import integrations
from app.routers.integrations import SyncDataRequest
from app.services import orcahub_sync
from app.services.integration_callback_url import APP_URL_SECRET_TAG, callback_url_tag
from tests.test_an_api_key_secret_rotates_in_place import (  # noqa: F401
    ADMIN,
    BASE,
    KEY,
    REAL_AUTHORIZE,
    _rotate,
    _stored,
    world,
)

APP = Path(__file__).resolve().parents[1] / "app"
ATTACKER = "https://collector.example.test"
HUB = "https://hub.example.test"


def _sync_body(app_url: str) -> SyncDataRequest:
    return SyncDataRequest.model_construct(
        app_url=app_url, organizations=[], members=[], include_clusters=False,
        include_projects=False, include_instances=False, cluster_ids=None,
    )


async def _sync(validated: Dict[str, Any], app_url: str, secret: Optional[str]) -> None:
    """The rest of a /sync whose dependency already validated the key with `secret`."""
    await integrations.sync_data(_sync_body(app_url), validated, None, None, secret)


def _org() -> Organization:
    return Organization(id=1, name="Own", slug="own", source="orcahub", external_id="ext-own", api_key_id=KEY,
                        created_by="system", is_active=True)


def _stored_url(world: Dict[str, Any]) -> Optional[str]:  # noqa: F811
    return world["vault"].docs[f"{BASE}/metadata"].get("app_url")


@pytest.fixture
def outbound(monkeypatch: pytest.MonkeyPatch) -> List[httpx.Request]:
    """Every request the refresh makes, answered with no organizations."""
    sent: List[httpx.Request] = []
    real_client = httpx.AsyncClient

    def _answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"organizations": []})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(_answer), **kw))
    return sent


async def _refresh(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(integrations, "_authorize_api_key", REAL_AUTHORIZE)
    return await integrations.refresh_organizations(api_key_id=KEY, db=None, user=ADMIN)


async def _no_callback_reaches(world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch,  # noqa: F811
                               outbound: List[httpx.Request]) -> None:
    """The member push and the webhook take their URL from here, and the refresh refuses."""
    assert await orcahub_sync._get_orcahub_credentials(_org()) is None
    with pytest.raises(HTTPException) as caught:
        await _refresh(monkeypatch)
    assert caught.value.status_code == 400
    assert outbound == []


# note: the race the review found

async def test_a_sync_in_flight_across_a_rotation_cannot_plant_its_url(
    world, monkeypatch, outbound  # noqa: F811
) -> None:
    validated = await integrations.validate_api_key(KEY, world["old"])
    answer = await _rotate()
    await _sync(validated, ATTACKER, world["old"])
    assert _stored_url(world) != ATTACKER, "a sync authenticated with the replaced secret set the URL"
    await _no_callback_reaches(world, monkeypatch, outbound)
    assert answer.api_key_secret not in repr(world["vault"].docs[f"{BASE}/metadata"])


async def test_a_url_stored_with_the_replaced_secret_is_never_used_with_the_new_one(
    world, monkeypatch, outbound  # noqa: F811
) -> None:
    """Even a write that slipped past the check: its tag names the secret it was stored with."""
    old_hash = world["vault"].docs[f"{BASE}/secret"]["hash"]
    await _rotate()
    world["vault"].docs[f"{BASE}/metadata"].update(app_url=ATTACKER, **{APP_URL_SECRET_TAG: callback_url_tag(old_hash)})
    await _no_callback_reaches(world, monkeypatch, outbound)


async def test_a_url_stored_before_the_binding_waits_for_the_next_sync(
    world, monkeypatch, outbound  # noqa: F811
) -> None:
    _stored(world, app_url=HUB)
    await _no_callback_reaches(world, monkeypatch, outbound)
    validated = await integrations.validate_api_key(KEY, world["old"])
    await _sync(validated, HUB, world["old"])
    creds = await orcahub_sync._get_orcahub_credentials(_org())
    assert creds == {"app_url": HUB, "api_key_id": KEY, "api_key_secret": world["old"]}


async def test_a_sync_with_the_current_secret_sets_the_url_every_callback_uses(
    world, monkeypatch, outbound  # noqa: F811
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
    await _refresh(monkeypatch)
    (request,) = outbound
    assert str(request.url) == f"{HUB}/api/external/organizations"
    assert request.headers["X-API-Key-Secret-Hash"] == world["vault"].docs[f"{BASE}/secret"]["hash"]


async def test_a_slow_url_write_keeps_the_key_lock_until_it_lands(world, monkeypatch) -> None:  # noqa: F811
    """The write used to run under a timeout. When Vault answered slowly the sync stopped waiting and
    left the lock while the thread went on, and its write of the whole key document, read before a
    revoke that took the lock meanwhile, put the revoked key back. Now the sync holds the lock until
    the write lands, so the revoke runs after it and stands.
    """
    import asyncio
    import threading

    from app.core import org_scope
    from tests._sqlite_session import org_tables, sqlite_session

    vault = world["vault"]
    writing, release = threading.Event(), threading.Event()
    real_put = vault.vault_kv_put

    def slow_put(path: str, data: Dict[str, Any]) -> None:
        if path == f"{BASE}/metadata" and data.get("app_url") == HUB and not release.is_set():
            writing.set()
            release.wait(5)
        real_put(path, data)

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
    assert doc["is_revoked"] is True, "the URL write undid the revoke"


@pytest.mark.parametrize("state", [{"is_revoked": True}, {"is_active": False}], ids=["revoked", "inactive"])
async def test_a_key_ended_while_the_sync_ran_gets_no_url(world, state: Dict[str, Any]) -> None:  # noqa: F811
    validated = await integrations.validate_api_key(KEY, world["old"])
    world["vault"].docs[f"{BASE}/metadata"].update(state)
    await _sync(validated, ATTACKER, world["old"])
    assert _stored_url(world) is None


async def test_a_sync_that_presents_no_secret_stores_no_url(world) -> None:  # noqa: F811
    validated = await integrations.validate_api_key(KEY, world["old"])
    await _sync(validated, ATTACKER, None)
    assert _stored_url(world) is None


# note: by shape, so a new reader of the stored URL cannot skip the binding

# Every function under app/ that reads a stored app_url, and why that is safe.
READERS = {
    "services/integration_callback_url.py:bound_callback_url": "the binding itself",
    "routers/integrations.py:_validate_api_key_blocking": "presence only: whether to backfill the raw secret",
    "routers/integrations.py:_forget_callback_url_blocking": "presence only: whether there is a URL to clear",
    "services/orcahub_sync.py:push_members_to_orcahub": "creds from _get_orcahub_credentials, which binds",
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
    # note: _get_orcahub_credentials tries each owner key through _credentials_of
    "services/orcahub_sync.py:_credentials_of",
    "routers/integrations.py:refresh_organizations",
])
def test_the_callbacks_take_their_url_from_the_binding(where: str) -> None:
    assert "bound_callback_url" in _calls(where)


@pytest.mark.parametrize("where", [
    "services/orcahub_sync.py:push_members_to_orcahub",
    "services/orcahub_webhook.py:emit_orcahub_event_durable",
    "services/orcahub_webhook.py:_emit",
])
def test_the_pushes_and_the_webhook_get_their_url_through_the_credentials(where: str) -> None:
    assert "_get_orcahub_credentials" in _calls(where)
