"""A key that owns an OrcaHub organization is not revoked or deleted.

Only the owner key syncs an organization, updates its members and policies and provisions for it,
and no other key can take it over. Revoking or deleting the owner therefore froze its
organizations, which is what happened on production on 2026-10-08. The key routes now refuse to
end an owner, for every caller, an Admin included, and say what to do instead: transfer the
organizations, delete them, or rotate the secret.

These send the real routes through their RBAC gates against SQLite organization rows and an
in-memory Vault with check-and-set, and assert the status, the structured detail, the audit
record, and that nothing was written, deleted or evicted.
"""

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional, Set

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core import org_scope, rbac
from app.core.auth import AuthenticatedUser, get_current_user
from app.core.database import get_db
from app.core.org_scope import org_scope_dependency
from app.core.vault_client import VaultCasMismatch, VaultClientError
from app.models.db_models import Organization, OrganizationMember
from app.routers import integrations
from app.services.integration_key_ownership import OWNERSHIP_UNKNOWN_DETAIL
from app.services.lxd_service import get_lxd_service
from tests._owner_key_world import RecordingLogger
from tests._sqlite_session import org_tables, sqlite_session

KEYS = "/api/v1/integrations/api-keys"
PLAIN, FREE, FOREIGN = "oak_plain", "oak_free", "oak_foreign"
ADMIN = AuthenticatedUser(user_id="a1", email="admin@example.test", groups=["role_admin"])
ALIAS_ADMIN = AuthenticatedUser(user_id="a2", email="alias@example.test", groups=["authentik Admins"])
PARTNER = AuthenticatedUser(user_id="p1", email="partner@example.test", groups=["role_partner"])
TENANT = AuthenticatedUser(user_id="t1", email="tenant@example.test", groups=["role_tenant"])
USERS = {"admin": ADMIN, "alias_admin": ALIAS_ADMIN}

ADMIN_REVOKE = (
    "This key owns 2 OrcaHub organizations (acme-cloud, nimbus-co). Revoking it would leave them with an "
    "owner no other key can replace, so their sync, member updates and provisioning would stop. "
    "Transfer them to another active key first, or delete them. To stop a leaked secret now, rotate "
    "the secret instead: the key ID and its organizations stay."
)


def path(key_id: str, leaf: str = "metadata") -> str:
    return f"secret/data/{integrations.VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/{leaf}"


def key(key_id: str, *, created_by: str = PARTNER.email, cluster: str = "c1", **over: Any) -> Dict[str, Any]:
    return {"id": key_id, "name": f"{key_id} connection", "app_name": "OrcaHub", "app_type": "orcahub",
            "created_by": created_by, "created_at": "2026-09-01T00:00:00", "expires_at": None,
            "is_active": True, "is_revoked": False,
            "cluster_permissions": [{"cluster_id": cluster, "permissions": ["admin"], "projects": ["*"]}], **over}


class Vault:
    """Key documents with KV v2 versions. Writes and deletes are recorded."""

    def __init__(self, *keys: Dict[str, Any]) -> None:
        self.docs: Dict[str, Dict[str, Any]] = {}
        self.versions: Dict[str, int] = {}
        for k in keys:
            self.docs[path(k["id"])] = dict(k)
            self.docs[path(k["id"], "secret")] = {"hash": "h", "raw": "oas_x"}
        self.writes: List[str] = []
        self.deletes: List[str] = []

    def vault_read(self, at: str, use_cache: bool = True) -> Dict[str, Any]:
        if at not in self.docs:
            raise VaultClientError("Vault read failed: 404 Client Error", status_code=404)
        return dict(self.docs[at])

    def vault_read_versioned(self, at: str) -> tuple:
        return (dict(self.docs[at]) if at in self.docs else None), self.versions.get(at, 1)

    def vault_kv_put(self, at: str, data: Dict[str, Any], cas: Any = None) -> None:
        if cas is not None and cas != self.versions.get(at, 1):
            raise VaultCasMismatch(at)
        self.writes.append(at)
        self.docs[at] = dict(data)
        self.versions[at] = self.versions.get(at, 1) + 1

    def vault_delete(self, at: str) -> None:
        self.deletes.append(at)
        self.docs.pop(at, None)


class Cache:
    def __init__(self) -> None:
        self.cleared: List[tuple] = []
        self.store: Dict[tuple, Any] = {}

    def get(self, ns: str, k: str) -> Any:
        return self.store.get((ns, k))

    def set(self, ns: str, k: str, value: Any, ttl: int = 0) -> None:
        self.store[(ns, k)] = value

    def delete(self, *args: Any) -> None:
        self.cleared.append(args)

    def delete_pattern(self, *args: Any) -> None:
        self.cleared.append(args)


def own(db: Any, key_id: str, slug: str, *, source: str = "orcahub", active: bool = True) -> int:
    db.add(Organization(name=slug.title(), slug=slug, source=source, external_id=f"ext-{slug}",
                        api_key_id=key_id, created_by="system", is_active=active))
    db.sync.commit()
    return db.sync.execute(select(Organization.id).where(Organization.slug == slug)).scalar_one()


@pytest.fixture
def owned(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """PLAIN owns acme-cloud and nimbus-co. The partner belongs to acme-cloud only."""
    vault = Vault(key(PLAIN), key(FREE), key(FOREIGN, created_by="someone@example.test", cluster="c9"))
    log, denials, cache = RecordingLogger(), RecordingLogger(), Cache()
    evicted: List[str] = []
    locks: List[str] = []
    real_lock = integrations.serialize_key_ownership

    @asynccontextmanager
    async def _seam(session: Any, key_id: str) -> AsyncIterator[None]:
        locks.append(key_id)
        async with real_lock(session, key_id):
            yield

    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: vault)
    monkeypatch.setattr(integrations, "activity_logger", log)
    monkeypatch.setattr(integrations, "get_cache_manager", lambda: cache)
    monkeypatch.setattr(integrations, "evict_api_key_cache", evicted.append)
    monkeypatch.setattr(integrations, "serialize_key_ownership", _seam)
    monkeypatch.setattr(integrations, "get_user_mappings_from_vault", lambda _v, _k: [])
    monkeypatch.setattr(integrations, "list_api_keys_from_vault",
                        lambda _v: [dict(vault.docs[path(k)]) for k in (PLAIN, FREE, FOREIGN) if path(k) in vault.docs])
    monkeypatch.setattr(rbac, "activity_logger", denials)
    db = sqlite_session(*org_tables())
    acme = own(db, PLAIN, "acme-cloud")
    own(db, PLAIN, "nimbus-co")
    db.add(OrganizationMember(organization_id=acme, user_email=PARTNER.email, role="partner"))
    db.sync.commit()
    return {"db": db, "vault": vault, "log": log, "denials": denials, "cache": cache, "evicted": evicted,
            "locks": locks}


def app_for(user: AuthenticatedUser, db: Any) -> FastAPI:
    app = FastAPI()
    app.include_router(integrations.router, prefix="/api/v1")
    scope: Optional[Set[str]] = None if user in (ADMIN, ALIAS_ADMIN) else {"c1"}

    async def _scope() -> None:
        org_scope.set_allowed_clusters(scope)

    async def _db() -> AsyncIterator[Any]:
        try:
            yield db
        finally:
            await db.rollback()

    async def _none() -> None:
        return None

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[org_scope_dependency] = _scope
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_lxd_service] = _none
    return app


async def send(owned: Dict[str, Any], user: AuthenticatedUser, method: str, at: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app_for(user, owned["db"]))
    async with httpx.AsyncClient(transport=transport, base_url="http://orcastra.test") as client:
        return await client.request(method, at)


def nothing_changed(owned: Dict[str, Any], key_id: str = PLAIN) -> None:
    vault = owned["vault"]
    assert vault.writes == [] and vault.deletes == [] and owned["evicted"] == []
    assert owned["cache"].cleared == []
    assert path(key_id) in vault.docs and vault.docs[path(key_id)]["is_revoked"] is False


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
    response = await send(owned, USERS[who], method, f"{KEYS}/{PLAIN}{suffix}")
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail == {
        "error": "key_owns_organizations", "action": action,
        "message": ADMIN_REVOKE if action == "revoke" else ADMIN_REVOKE.replace("Revoking it", "Deleting it"),
        "api_key_id": PLAIN, "organization_count": 2, "organization_slugs": ["acme-cloud", "nimbus-co"],
        "hidden_organization_count": 0, "remedies": ["transfer_owner", "delete_organization", "rotate_secret"],
    }
    nothing_changed(owned)
    record = denial(owned)
    assert (record["action"], record["target_id"]) == (audited, PLAIN)
    assert record["details"]["owned_organization_slugs"] == ["acme-cloud", "nimbus-co"]
    assert record["details"]["owned_organization_count"] == 2
    assert record["details"]["owned_organization_hidden_count"] == 0
    assert owned["locks"] == [PLAIN]


@pytest.mark.parametrize("method, suffix", [("POST", "/revoke"), ("DELETE", "")], ids=["revoke", "delete"])
async def test_a_partner_sees_only_the_organizations_it_belongs_to(
    owned: Dict[str, Any], method: str, suffix: str
) -> None:
    response = await send(owned, PARTNER, method, f"{KEYS}/{PLAIN}{suffix}")
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert (detail["organization_count"], detail["organization_slugs"], detail["hidden_organization_count"]) == (
        2, ["acme-cloud"], 1,
    )
    assert "nimbus-co" not in response.text, "a partner was told the slug of an organization it cannot see"
    assert "(acme-cloud and 1 you cannot see)" in detail["message"]
    assert "Ask an administrator to transfer them" in detail["message"]
    nothing_changed(owned)
    # note: the record names what the answer named. The caller who triggered it can read it.
    record = denial(owned)
    assert (record["details"]["owned_organization_slugs"], record["details"]["owned_organization_count"],
            record["details"]["owned_organization_hidden_count"]) == (["acme-cloud"], 2, 1)
    assert "nimbus-co" not in repr(record)


async def test_one_organization_reads_in_the_singular(owned: Dict[str, Any]) -> None:
    owned["db"].sync.query(Organization).filter(Organization.slug == "nimbus-co").delete()
    owned["db"].sync.commit()
    detail = (await send(owned, ADMIN, "POST", f"{KEYS}/{PLAIN}/revoke")).json()["detail"]
    assert detail["message"].startswith(
        "This key owns 1 OrcaHub organization (acme-cloud). Revoking it would leave it with an owner"
    )
    assert "so its sync, member updates and provisioning would stop. Transfer it to another" in detail["message"]


async def test_inactive_and_dashboard_organizations_do_not_count(owned: Dict[str, Any]) -> None:
    owned["db"].sync.query(Organization).filter(Organization.slug == "acme-cloud").update({"is_active": False})
    owned["db"].sync.query(Organization).filter(Organization.slug == "nimbus-co").update({"source": "local"})
    owned["db"].sync.commit()
    response = await send(owned, ADMIN, "POST", f"{KEYS}/{PLAIN}/revoke")
    assert response.status_code == 200, response.text
    assert owned["vault"].docs[path(PLAIN)]["is_revoked"] is True


@pytest.mark.parametrize("method, suffix", [("POST", "/revoke"), ("DELETE", "")], ids=["revoke", "delete"])
@pytest.mark.parametrize("user", [ADMIN, PARTNER], ids=["admin", "partner"])
async def test_a_key_that_owns_nothing_ends_as_before(
    owned: Dict[str, Any], method: str, suffix: str, user: AuthenticatedUser
) -> None:
    response = await send(owned, user, method, f"{KEYS}/{FREE}{suffix}")
    assert response.status_code == 200, response.text
    if method == "POST":
        assert owned["vault"].docs[path(FREE)]["is_revoked"] is True
    else:
        assert path(FREE) not in owned["vault"].docs
    assert owned["evicted"], "the key's cached copies were not dropped"
    (record,) = owned["log"].calls
    assert record["result"] == "success" and record["target_id"] == FREE
    assert owned["locks"] == [FREE]


@pytest.mark.parametrize("method, suffix", [("POST", "/revoke"), ("DELETE", "")], ids=["revoke", "delete"])
async def test_unreadable_ownership_is_503_and_changes_nothing(
    owned: Dict[str, Any], method: str, suffix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_execute = owned["db"].execute

    async def _down(statement: Any, *args: Any, **kwargs: Any) -> Any:
        if "organizations" in str(statement) and "api_key_id IN" in str(statement):
            raise RuntimeError("connection to server at db.internal failed")
        return await real_execute(statement, *args, **kwargs)

    monkeypatch.setattr(owned["db"], "execute", _down)
    response = await send(owned, ADMIN, method, f"{KEYS}/{PLAIN}{suffix}")
    assert response.status_code == 503
    assert response.json()["detail"] == OWNERSHIP_UNKNOWN_DETAIL
    assert "db.internal" not in response.text
    nothing_changed(owned)


async def test_a_tenant_never_reaches_the_key(owned: Dict[str, Any]) -> None:
    for method, suffix in (("POST", "/revoke"), ("DELETE", "")):
        assert (await send(owned, TENANT, method, f"{KEYS}/{PLAIN}{suffix}")).status_code == 403
    nothing_changed(owned)
    assert owned["log"].calls == []


async def test_a_key_outside_the_scope_is_404_before_ownership_is_read(owned: Dict[str, Any]) -> None:
    response = await send(owned, PARTNER, "POST", f"{KEYS}/{FOREIGN}/revoke")
    assert response.status_code == 404
    assert owned["locks"] == [] and owned["log"].calls == []


# note: what the key routes say a key owns

async def test_the_list_names_what_each_key_owns(owned: Dict[str, Any]) -> None:
    admin = {k["id"]: k for k in (await send(owned, ADMIN, "GET", KEYS)).json()["keys"]}
    assert (admin[PLAIN]["owned_organization_count"], admin[PLAIN]["owned_organization_slugs"],
            admin[PLAIN]["owned_organization_hidden_count"]) == (2, ["acme-cloud", "nimbus-co"], 0)
    assert (admin[FREE]["owned_organization_count"], admin[FREE]["owned_organization_slugs"]) == (0, [])
    partner = {k["id"]: k for k in (await send(owned, PARTNER, "GET", KEYS)).json()["keys"]}
    assert set(partner) == {PLAIN, FREE}
    assert (partner[PLAIN]["owned_organization_count"], partner[PLAIN]["owned_organization_slugs"],
            partner[PLAIN]["owned_organization_hidden_count"]) == (2, ["acme-cloud"], 1)


async def test_the_detail_names_what_the_key_owns(owned: Dict[str, Any]) -> None:
    body = (await send(owned, PARTNER, "GET", f"{KEYS}/{PLAIN}")).json()
    assert (body["owned_organization_count"], body["owned_organization_slugs"],
            body["owned_organization_hidden_count"]) == (2, ["acme-cloud"], 1)


async def test_ownership_is_read_after_the_shared_list_cache(owned: Dict[str, Any]) -> None:
    await send(owned, ADMIN, "GET", KEYS)
    cached = [v for (ns, _k), v in owned["cache"].store.items() if ns == "api_responses"]
    assert cached and all("owned_organization_count" not in row for rows in cached for row in rows)


async def test_an_unreadable_ownership_leaves_the_list_working(
    owned: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _down(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("database down")

    monkeypatch.setattr(integrations, "owned_by_keys", _down)
    response = await send(owned, ADMIN, "GET", KEYS)
    assert response.status_code == 200
    assert all(k["owned_organization_count"] is None for k in response.json()["keys"])


# note: the way out

async def test_after_a_transfer_the_old_key_can_be_revoked(owned: Dict[str, Any]) -> None:
    transport = httpx.ASGITransport(app=app_for(ADMIN, owned["db"]))
    async with httpx.AsyncClient(transport=transport, base_url="http://orcastra.test") as client:
        ids = owned["db"].sync.execute(select(Organization.id).where(Organization.api_key_id == PLAIN)).scalars().all()
        for org_id in ids:
            moved = await client.put(f"/api/v1/integrations/organizations/{org_id}/owner-key",
                                     json={"api_key_id": FREE, "reason": "replacing a leaked connection key"})
            assert moved.status_code == 200, moved.text
        revoked = await client.post(f"{KEYS}/{PLAIN}/revoke")
    assert revoked.status_code == 200, revoked.text
    assert owned["vault"].docs[path(PLAIN)]["is_revoked"] is True
