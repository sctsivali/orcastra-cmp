"""GET /integrations/organizations says how each organization's owner key stands.

The owner key is the only key that syncs an organization, updates its members and policies and
provisions for it, so an organization whose owner is revoked, expired, deleted or unreadable is
frozen. The list could not say so: it named the key id and the application, read through a helper
that also loaded the key's secret document. It now reads each owner's metadata only, live rather
than from the shared cache, and reports the owner's name, state, expiry and the organization's
clusters. Membership still scopes what a Partner sees.
"""

from datetime import datetime, timedelta
from typing import Any, AsyncIterator, Dict, List

import httpx
import pytest
from fastapi import FastAPI

from app.core.auth import AuthenticatedUser, get_current_user
from app.core.database import get_db
from app.core.org_scope import org_scope_dependency
from app.core.vault_client import VaultClientError
from app.models.db_models import Organization, OrganizationCluster, OrganizationMember
from app.routers import integrations
from app.services.lxd_service import get_lxd_service
from tests._sqlite_session import org_tables, sqlite_session

_HTTP = httpx.AsyncClient
ADMIN = AuthenticatedUser(user_id="a1", email="admin@example.test", groups=["role_admin"])
PARTNER = AuthenticatedUser(user_id="p1", email="partner@example.test", groups=["role_partner"])


def path(key_id: str, leaf: str = "metadata") -> str:
    return f"secret/data/{integrations.VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/{leaf}"


def key(key_id: str, **over: Any) -> Dict[str, Any]:
    return {"id": key_id, "name": f"{key_id} name", "app_name": "OrcaHub", "app_type": "orcahub",
            "is_active": True, "is_revoked": False, "expires_at": None, **over}


# note: naive UTC, the shape create_api_key writes on this line.
SOON = (datetime.utcnow() + timedelta(days=3)).isoformat()
LATER = (datetime.utcnow() + timedelta(days=200)).isoformat()
OWNERS = {
    "active": key("oak_active", expires_at=LATER),
    "expiring": key("oak_expiring", expires_at=SOON),
    "expired": key("oak_expired", expires_at="2020-01-01T00:00:00+00:00"),
    "revoked": key("oak_revoked", is_revoked=True, is_active=False),
    "inactive": key("oak_inactive", is_active=False),
}
UNREADABLE = "oak_unknown"
MISSING = "oak_missing"


class Vault:
    def __init__(self) -> None:
        self.docs = {path(k["id"]): k for k in OWNERS.values()}
        self.reads: List[tuple] = []

    def vault_read(self, at: str, use_cache: bool = True) -> Dict[str, Any]:
        self.reads.append((at, use_cache))
        if at == path(UNREADABLE):
            raise VaultClientError("Vault read failed: 503 Server Error", status_code=503)
        if at not in self.docs:
            raise VaultClientError("Vault read failed: 404 Client Error", status_code=404)
        return dict(self.docs[at])


@pytest.fixture
def listing(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    vault = Vault()
    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: vault)
    db = sqlite_session(*org_tables())
    owners = {state: meta["id"] for state, meta in OWNERS.items()}
    owners.update({"missing": MISSING, "unknown": UNREADABLE, "unclaimed": None})
    for state, owner in owners.items():
        db.add(Organization(name=state.title(), slug=f"org-{state}", source="orcahub", external_id=f"ext-{state}",
                            api_key_id=owner, created_by="system", is_active=True))
    db.add(Organization(name="Local", slug="org-local", source="local", created_by=PARTNER.email))
    db.sync.commit()
    ids = {slug: oid for oid, slug in db.sync.execute(Organization.__table__.select().with_only_columns(
        Organization.id, Organization.slug)).all()}
    db.add_all([OrganizationCluster(organization_id=ids["org-active"], cluster_id="c2"),
                OrganizationCluster(organization_id=ids["org-active"], cluster_id="c1"),
                OrganizationMember(organization_id=ids["org-revoked"], user_email=PARTNER.email, role="partner")])
    db.sync.commit()
    return {"db": db, "vault": vault, "ids": ids}


async def get(listing: Dict[str, Any], user: AuthenticatedUser) -> List[Dict[str, Any]]:
    app = FastAPI()
    app.include_router(integrations.router, prefix="/api/v1")

    async def _db() -> AsyncIterator[Any]:
        yield listing["db"]

    async def _none() -> None:
        return None

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[org_scope_dependency] = _none
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_lxd_service] = _none
    async with _HTTP(transport=httpx.ASGITransport(app=app), base_url="http://orcastra.test") as client:
        response = await client.get("/api/v1/integrations/organizations")
    assert response.status_code == 200, response.text
    return response.json()


async def test_each_owner_state_is_named(listing: Dict[str, Any]) -> None:
    rows = {row["slug"]: row for row in await get(listing, ADMIN)}
    assert "org-local" not in rows, "a dashboard organization has no owner key and is not listed"
    states = {slug: row["owner_key_state"] for slug, row in rows.items()}
    assert states == {
        "org-active": "active", "org-expiring": "expiring", "org-expired": "expired", "org-revoked": "revoked",
        "org-inactive": "inactive", "org-missing": "missing", "org-unknown": "unknown", "org-unclaimed": "unclaimed",
    }


async def test_the_owner_key_is_described(listing: Dict[str, Any]) -> None:
    rows = {row["slug"]: row for row in await get(listing, ADMIN)}
    active = rows["org-active"]
    assert (active["api_key_id"], active["owner_key_name"], active["app_name"]) == (
        "oak_active", "oak_active name", "OrcaHub",
    )
    assert (active["owner_key_expires_at"], active["owner_key_expiry_state"]) == (LATER, "active")
    assert active["cluster_ids"] == ["c1", "c2"]
    assert rows["org-expiring"]["owner_key_expiry_state"] == "expiring"
    for slug in ("org-missing", "org-unknown", "org-unclaimed"):
        row = rows[slug]
        assert (row["owner_key_name"], row["app_name"], row["owner_key_expires_at"], row["owner_key_expiry_state"]) == (
            None, None, None, None,
        )
        assert row["cluster_ids"] == []


async def test_only_metadata_is_read_and_each_key_once(listing: Dict[str, Any]) -> None:
    await get(listing, ADMIN)
    reads = listing["vault"].reads
    assert reads and all(at.endswith("/metadata") for at, _ in reads), "the list read a key's secret document"
    assert all(use_cache is False for _, use_cache in reads), "a cached copy can predate a revoke"
    assert len(reads) == len({at for at, _ in reads}) == 7


async def test_a_partner_sees_only_organizations_it_belongs_to(listing: Dict[str, Any]) -> None:
    rows = await get(listing, PARTNER)
    assert [(row["slug"], row["owner_key_state"]) for row in rows] == [("org-revoked", "revoked")]
    assert [at for at, _ in listing["vault"].reads] == [path("oak_revoked")]


async def test_an_empty_list_reads_no_key(listing: Dict[str, Any]) -> None:
    outsider = AuthenticatedUser(user_id="p2", email="nobody@example.test", groups=["role_partner"])
    assert await get(listing, outsider) == []
    assert listing["vault"].reads == []
