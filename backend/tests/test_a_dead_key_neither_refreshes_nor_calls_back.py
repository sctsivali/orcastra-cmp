"""A revoked, inactive or expired key is neither refreshed nor called back.

refresh-organizations checked only the key's scope and its app_url, so a dead key's stored
credential hash was still sent to the key's callback URL and the answer was written as
organizations under the dead key's ownership. The OrcaHub callbacks (the member push, the
removal push and the lifecycle webhook) read the same key and sent its raw secret out for it.
Both now stop at the key's state: the refresh answers 409 before any outbound call, and the
callback credentials are None before the secret is read.
"""

from typing import Any, AsyncIterator, Dict, List

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.auth import AuthenticatedUser, get_current_user
from app.core.database import get_db
from app.core.org_scope import org_scope_dependency
from app.models.db_models import Organization
from app.routers import integrations
from app.services import orcahub_sync
from app.services.lxd_service import get_lxd_service
from tests._sqlite_session import org_tables, sqlite_session

_HTTP = httpx.AsyncClient
KEY = "oak_dead"
CLUSTER = "cluster-b"
PARTNER = AuthenticatedUser(user_id="p1", email="partner@example.test", groups=["role_partner"])
BASE = f"secret/data/{integrations.VAULT_INTEGRATIONS_PATH}/api_keys/{KEY}"
DEAD = [
    {"is_revoked": True},
    {"is_active": False},
    {"expires_at": "2020-01-01T00:00:00"},
    {"expires_at": "not-a-date"},
]
DEAD_IDS = ["revoked", "inactive", "expired", "unreadable-expiry"]


def metadata(**state: Any) -> Dict[str, Any]:
    return {
        "id": KEY, "name": "hub", "app_name": "OrcaHub", "app_type": "orcahub",
        "capabilities": ["inventory.read", "org.sync", "tenant.provision"],
        "created_by": PARTNER.email, "created_at": "2026-09-01T00:00:00+00:00",
        "is_active": True, "is_revoked": False, "expires_at": None,
        "app_url": "https://hub.example.test",
        "cluster_permissions": [{"cluster_id": CLUSTER, "permissions": ["admin"], "projects": ["*"]}],
        **state,
    }


class Vault:
    """The key as Vault holds it, with every path read recorded."""

    def __init__(self, meta: Dict[str, Any]) -> None:
        self.docs = {f"{BASE}/metadata": meta, f"{BASE}/secret": {"hash": "hash-do-not-send", "raw": "oas_raw"}}
        self.reads: List[str] = []

    def vault_read(self, path: str, use_cache: bool = True) -> Dict[str, Any]:
        self.reads.append(path)
        return dict(self.docs[path])


@pytest.fixture
def outbound(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """Every outbound request the handlers make, answered with one organization."""
    calls: List[str] = []

    def _answer(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"organizations": [{"id": "ext-1", "name": "Hub Org", "slug": "hub-org"}]})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _HTTP(transport=httpx.MockTransport(_answer), **kw))
    return calls


def use(monkeypatch: pytest.MonkeyPatch, meta: Dict[str, Any]) -> Vault:
    vault = Vault(meta)
    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: vault)
    monkeypatch.setattr(integrations, "get_current_allowed_clusters", lambda: {CLUSTER})
    return vault


async def refresh(db: Any) -> httpx.Response:
    app = FastAPI()
    app.include_router(integrations.router, prefix="/api/v1")

    async def _db() -> AsyncIterator[Any]:
        try:
            yield db
        finally:
            await db.rollback()

    async def _none() -> None:
        return None

    app.dependency_overrides[get_current_user] = lambda: PARTNER
    app.dependency_overrides[org_scope_dependency] = _none
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_lxd_service] = _none
    async with _HTTP(transport=httpx.ASGITransport(app=app), base_url="http://orcastra.test") as client:
        return await client.post(f"/api/v1/integrations/refresh-organizations?api_key_id={KEY}")


async def orgs(db: Any) -> List[tuple]:
    return sorted((await db.execute(select(Organization.slug, Organization.api_key_id))).all())


@pytest.mark.parametrize("state", DEAD, ids=DEAD_IDS)
async def test_a_dead_key_is_not_refreshed(monkeypatch, outbound: List[str], state: Dict[str, Any]) -> None:
    vault = use(monkeypatch, metadata(**state))
    db = sqlite_session(*org_tables())
    response = await refresh(db)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "This API key is revoked, inactive or expired, so it cannot be refreshed"
    assert outbound == [], "a dead key's callback URL must never be called"
    # note: the scope check reads the key through get_api_key_from_vault, which loads the secret
    # document's hash with it. The refresh's own read, the one that feeds the callback, must not run.
    assert vault.reads.count(f"{BASE}/secret") <= 1, "the refresh read the credential it would have sent"
    assert await orgs(db) == [], "nothing may be written under a dead key's ownership"


async def test_a_live_key_still_refreshes(monkeypatch, outbound: List[str]) -> None:
    use(monkeypatch, metadata(expires_at="2999-01-01T00:00:00"))
    db = sqlite_session(*org_tables())
    response = await refresh(db)
    assert response.status_code == 200, response.text
    assert response.json()["organizations_synced"] == 1
    assert outbound == ["https://hub.example.test/api/external/organizations"]
    assert await orgs(db) == [("hub-org", KEY)]


async def test_an_out_of_scope_dead_key_is_still_404(monkeypatch, outbound: List[str]) -> None:
    """The state check runs after the scope check, so it never confirms an id the caller cannot see."""
    use(monkeypatch, metadata(is_revoked=True))
    monkeypatch.setattr(integrations, "get_current_allowed_clusters", lambda: {"cluster-other"})
    response = await refresh(sqlite_session(*org_tables()))
    assert response.status_code == 404 and outbound == []


def _org(**over: Any) -> Organization:
    return Organization(id=1, name="Hub Org", slug="hub-org", source="orcahub", external_id="ext-1",
                        api_key_id=KEY, created_by="system", is_active=True, **over)


@pytest.mark.parametrize("state", DEAD, ids=DEAD_IDS)
async def test_a_dead_key_gets_no_callback_credentials(monkeypatch, state: Dict[str, Any]) -> None:
    vault = use(monkeypatch, metadata(**state))
    assert await orcahub_sync._get_orcahub_credentials(_org()) is None
    assert vault.reads == [f"{BASE}/metadata"], "the raw secret was read for a key that gets no callback"


async def test_a_live_key_still_gets_its_callback_credentials(monkeypatch) -> None:
    use(monkeypatch, metadata())
    creds = await orcahub_sync._get_orcahub_credentials(_org())
    assert creds == {"app_url": "https://hub.example.test", "api_key_id": KEY, "api_key_secret": "oas_raw"}


@pytest.mark.parametrize("state", DEAD, ids=DEAD_IDS)
async def test_a_dead_key_pushes_no_members(monkeypatch, outbound: List[str], state: Dict[str, Any]) -> None:
    use(monkeypatch, metadata(**state))
    db = sqlite_session(*org_tables())
    db.add(_org())
    await db.commit()
    pushed = await orcahub_sync.push_members_to_orcahub(db, 1, [{"email": "t@example.test", "role": "tenant"}])
    assert pushed is False and outbound == []
