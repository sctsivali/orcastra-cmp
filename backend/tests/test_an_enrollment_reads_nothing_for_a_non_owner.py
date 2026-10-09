"""Enrollment decides that the calling key owns the organization before it reads anything of the new key.

Checking the new key's secret first made the route an oracle for any integration key: a wrong
secret and a non-owner answered differently, a stranger's failures locked a victim key out of
enrollment, and spent the organization's budget. Now a non-owner, a missing organization and an
unclaimed one get one 403 with no Vault read of the new key, failures count per (vouching key, new key),
the organization's budget is spent only by an owner, an unreadable store is 503 and counts nothing, and
ownership is decided under the organization lock.

Also: with INTEGRATION_MULTI_OWNER_ENABLED off the routes answer 404 before the key is authenticated or
the body read, and a 422 never carries the secret.
"""

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from app.core.database import get_db
from app.routers import integrations
from app.services import owner_key_enrollment
from app.services.lxd_service import get_lxd_service
from app.services.organization_owners import owner_key_ids
from tests._owner_invariants import assert_owner_invariants
from tests.test_a_key_that_owns_an_organization_is_not_revoked_or_deleted import path as key_path
from tests.test_an_owner_key_vouches_for_a_key_it_holds import (  # noqa: F401
    EXT,
    NEW,
    NEW_SECRET,
    OTHER,
    OWNER,
    OWNER_KEYS,
    call,
    enroll,
    world,
)

NOT_AN_OWNER = {"error": "not_an_owner", "message": owner_key_enrollment.NOT_AN_OWNER}


@pytest.fixture
def reads(world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> List[str]:  # noqa: F811
    """Every Vault path read, so a test can say the new key was never looked at."""
    seen: List[str] = []
    real = world["vault"].vault_read

    def recording(path: str, use_cache: bool = True) -> Dict[str, Any]:
        seen.append(path)
        return real(path, use_cache)

    monkeypatch.setattr(world["vault"], "vault_read", recording)
    return seen


def read_the_new_key(reads: List[str]) -> bool:
    return any(f"/api_keys/{NEW}/" in path for path in reads)


def unclaim(world: Dict[str, Any]) -> None:  # noqa: F811
    world["db"].sync.execute(text("UPDATE organizations SET api_key_id = NULL"))
    world["db"].sync.execute(text("DELETE FROM organization_owner_keys"))
    world["db"].sync.commit()


@pytest.mark.parametrize("case", ["not_owner", "missing_org", "unclaimed_org"])
async def test_a_non_owner_gets_one_answer_and_reads_nothing(
    world: Dict[str, Any], reads: List[str], case: str  # noqa: F811
) -> None:
    caller, ext = (OTHER, EXT) if case == "not_owner" else (OWNER, "ext-nowhere" if case == "missing_org" else EXT)
    if case == "unclaimed_org":
        unclaim(world)
    for secret in (NEW_SECRET, "wrong"):
        response = await call(world, caller, "POST", OWNER_KEYS, enroll(secret=secret, ext=ext))
        assert (response.status_code, response.json()["detail"]) == (403, NOT_AN_OWNER)
    assert not read_the_new_key(reads), "the new key was read for a caller that owns nothing"


async def test_a_stranger_spends_neither_the_organizations_budget_nor_the_lockout(
    world: Dict[str, Any],  # noqa: F811
) -> None:
    for _ in range(owner_key_enrollment.PER_MINUTE):
        response = await call(world, OTHER, "POST", OWNER_KEYS, enroll(secret="wrong"))
        assert response.status_code == 403
    assert not owner_key_enrollment.locked_out(OTHER, NEW) and not owner_key_enrollment.locked_out(OWNER, NEW)
    response = await call(world, OWNER, "POST", OWNER_KEYS, enroll())
    assert response.status_code == 200 and response.json()["added"] is True


@pytest.mark.parametrize("path", ["metadata", "secret"])
async def test_an_unreadable_store_is_503_and_counts_nothing(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, path: str  # noqa: F811
) -> None:
    real = world["vault"].vault_read

    def down(read: str, use_cache: bool = True) -> Dict[str, Any]:
        if read == key_path(NEW, path):
            raise ConnectionError("vault.internal:8200 refused")
        return real(read, use_cache)

    monkeypatch.setattr(world["vault"], "vault_read", down)
    for _ in range(owner_key_enrollment.FAILURES_BEFORE_LOCKOUT):
        response = await call(world, OWNER, "POST", OWNER_KEYS, enroll())
        assert (response.status_code, response.json()["detail"]) == (503, owner_key_enrollment.UNREADABLE)
    assert not owner_key_enrollment.locked_out(OWNER, NEW)
    assert "vault.internal" not in response.text


async def test_an_owner_removed_while_waiting_cannot_enroll(
    world: Dict[str, Any], reads: List[str], monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """An Administrator's transfer to OTHER lands while the enrollment waits for the organization lock."""
    real = integrations.serialize_org_owners

    @asynccontextmanager
    async def transfer_lands_first(session: Any, org_ids: Any) -> AsyncIterator[None]:
        session.sync.execute(text("UPDATE organizations SET api_key_id = :key"), {"key": OTHER})
        session.sync.execute(text("UPDATE organization_owner_keys SET api_key_id = :key"), {"key": OTHER})
        session.sync.commit()
        async with real(session, org_ids):
            yield

    monkeypatch.setattr(integrations, "serialize_org_owners", transfer_lands_first)
    response = await call(world, OWNER, "POST", OWNER_KEYS, enroll())
    assert (response.status_code, response.json()["detail"]) == (403, NOT_AN_OWNER)
    assert await owner_key_ids(world["db"], world["org"]) == [OTHER]
    assert not read_the_new_key(reads)
    await assert_owner_invariants(world["db"])


def real_auth_app(db: Any) -> FastAPI:
    """The external router with its real key authentication: only the session is faked."""
    app = FastAPI()
    app.include_router(integrations.external_router, prefix="/api/v1")

    async def _db() -> AsyncIterator[Any]:
        yield db

    async def _none() -> None:
        return None

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_lxd_service] = _none
    return app


@pytest.mark.parametrize("method, path, content", [
    ("GET", f"/api/v1/integrations/external/organizations/{EXT}/owner-keys", None),
    ("POST", OWNER_KEYS, b'{"organization_external_id": '),
    ("POST", OWNER_KEYS, b"{}"),
    ("DELETE", f"/api/v1/integrations/external/organizations/{EXT}/owner-keys/self", None),
], ids=["read", "enroll_bad_json", "enroll_empty", "leave"])
async def test_with_the_flag_off_the_routes_are_404_before_the_key_or_the_body(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, method: str, path: str, content: Any  # noqa: F811
) -> None:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=real_auth_app(world["db"]))
        async with httpx.AsyncClient(transport=transport, base_url="http://orcastra.test") as client:
            return await client.request(method, path, content=content, headers={
                "content-type": "application/json", "X-API-Key-ID": OWNER, "X-API-Key-Secret": "not-its-secret"})

    monkeypatch.setattr(integrations, "multi_owner_enabled", lambda: False)
    assert (await send()).status_code == 404
    monkeypatch.setattr(integrations, "multi_owner_enabled", lambda: True)
    assert (await send()).status_code == 401, "with the flag on, the wrong secret is what answers"


@pytest.mark.parametrize("body", [
    {"organization_external_id": EXT, "new_key_id": NEW, "new_key_secret": NEW_SECRET + "x" * 600},
    {"organization_external_id": "", "new_key_id": NEW, "new_key_secret": NEW_SECRET},
    {"organization_external_id": EXT, "new_key_id": "not a key id!", "new_key_secret": NEW_SECRET},
    [NEW_SECRET],
], ids=["secret_too_long", "other_field", "bad_key_id", "not_an_object"])
async def test_a_422_never_carries_the_secret(world: Dict[str, Any], body: Any) -> None:  # noqa: F811
    response = await call(world, OWNER, "POST", OWNER_KEYS, body)
    assert response.status_code == 422
    assert NEW_SECRET not in response.text
    assert all(set(error) == {"type", "loc", "msg"} for error in response.json()["detail"])


async def test_the_secret_is_kept_out_of_the_models_repr() -> None:
    parsed = integrations.OwnerKeyEnrollRequest(organization_external_id=EXT, new_key_id=NEW, new_key_secret=NEW_SECRET)
    assert NEW_SECRET not in repr(parsed) and parsed.new_key_secret.get_secret_value() == NEW_SECRET


async def test_the_published_spec_still_describes_the_body() -> None:
    app = FastAPI()
    app.include_router(integrations.external_router, prefix="/api/v1")
    operation = app.openapi()["paths"]["/api/v1/integrations/external/organizations/owner-keys"]["post"]
    schema = operation["requestBody"]["content"]["application/json"]["schema"]
    assert schema["required"] == ["organization_external_id", "new_key_id", "new_key_secret"]
    assert schema["properties"]["new_key_secret"]["writeOnly"] is True

