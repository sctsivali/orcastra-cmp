"""An Administrator moves a synced organization to another owner key.

organizations.api_key_id names the key that owns a synced organization, and only the owner may
sync it, write its members and policies, and provision for it. Nothing moved an owner, so a
revoked, deleted or lapsed owner key froze its organizations until someone ran SQL against
production. PUT /integrations/organizations/{id}/owner-key is that move, for an Admin only, with
the new key judged inside the per-key ownership lock and the move audited.

These send the real route through its real RBAC and read-only gates against a SQLite database,
an in-memory Vault and a recording audit log, then prove the move through the external sync,
member and provisioning routes.
"""

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core import dependencies, rbac
from app.core.auth import AuthenticatedUser, get_current_user
from app.core.database import get_db
from app.core.org_scope import org_scope_dependency
from app.core.vault_client import VaultClientError
from app.models.db_models import Organization, OrganizationCluster, OrganizationMember
from app.routers import integrations
from app.services import authentik_sync, integration_key_ownership
from app.services.activity_logger import ACTION_CATEGORIES, HIGH_SEVERITY_ACTIONS, EventCategory
from app.services.integration_roles import ROLE_PARTNER
from app.services.lxd_service import get_lxd_service
from tests._owner_key_world import (
    PROVISION,
    SALE,
    SELLER,
    Directory,
    RecordingLogger,
    provision_body,
    send,
    world_db,
)

_HTTP = httpx.AsyncClient
K_OLD, K_NEW = "oak_old_owner", "oak_new_owner"
SLUG = "acme-cloud"
ADMIN = AuthenticatedUser(user_id="a1", email="admin@example.test", groups=["role_admin"])
ALIAS_ADMIN = AuthenticatedUser(user_id="a2", email="alias@example.test", groups=["authentik Admins"])
PARTNER = AuthenticatedUser(user_id="p1", email="partner@example.test", groups=["role_partner"])
TENANT = AuthenticatedUser(user_id="t1", email="tenant@example.test", groups=["role_tenant"])
REASON = "The previous connection key was revoked on 2026-10-08"


def path(key_id: str, leaf: str = "metadata") -> str:
    return f"secret/data/{integrations.VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/{leaf}"


def key(key_id: str, **over: Any) -> Dict[str, Any]:
    return {
        "id": key_id, "name": f"{key_id} connection", "app_name": "OrcaHub", "app_type": "orcahub",
        "created_by": "partner@example.test", "created_at": "2026-09-01T00:00:00",
        "expires_at": None, "is_active": True, "is_revoked": False,
        "cluster_permissions": [{"cluster_id": SALE, "permissions": ["admin"], "projects": ["*"]}],
        **over,
    }


class Vault:
    """Key documents as Vault holds them. Every read is recorded with whether it allowed the cache."""

    def __init__(self, *keys: Dict[str, Any]) -> None:
        self.docs = {path(k["id"]): dict(k) for k in keys}
        self.reads: List[tuple] = []
        self.down = False

    def vault_read(self, at: str, use_cache: bool = True) -> Dict[str, Any]:
        self.reads.append((at, use_cache))
        if self.down:
            raise VaultClientError("Vault read failed: connection refused")
        if at not in self.docs:
            raise VaultClientError("Vault read failed: 404 Client Error", status_code=404)
        return dict(self.docs[at])


class Cache:
    def __init__(self) -> None:
        self.cleared: List[tuple] = []

    def delete_pattern(self, *args: Any) -> None:
        self.cleared.append(args)

    def delete(self, *args: Any) -> None:
        self.cleared.append(args)

    def get(self, *_a: Any) -> None:
        return None

    def set(self, *_a: Any, **_k: Any) -> None:
        return None


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """The old owner is revoked, the new one is an active OrcaHub key. The org is bound to SALE."""
    vault = Vault(key(K_OLD, is_revoked=True, is_active=False), key(K_NEW))
    log, denials, cache = RecordingLogger(), RecordingLogger(), Cache()
    evicted: List[str] = []
    locks: List[str] = []
    real_lock = integrations.serialize_key_ownership

    @asynccontextmanager
    async def _seam(db: Any, key_id: str) -> AsyncIterator[None]:
        locks.append(key_id)
        async with real_lock(db, key_id):
            yield

    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: vault)
    monkeypatch.setattr(integrations, "activity_logger", log)
    monkeypatch.setattr(integrations, "get_cache_manager", lambda: cache)
    monkeypatch.setattr(integrations, "evict_api_key_cache", evicted.append)
    monkeypatch.setattr(integrations, "serialize_key_ownership", _seam)
    monkeypatch.setattr(rbac, "activity_logger", denials)
    directory = Directory({"partner@example.test": {ROLE_PARTNER}})
    monkeypatch.setattr(authentik_sync, "get_authentik_sync", lambda: directory)

    db = world_db()
    db.add(Organization(name="Acme Cloud", slug=SLUG, source="orcahub", external_id=SELLER, api_key_id=K_OLD,
                        created_by="system", is_active=True))
    db.sync.commit()
    org_id = db.sync.execute(select(Organization.id).where(Organization.slug == SLUG)).scalar_one()
    db.add(OrganizationCluster(organization_id=org_id, cluster_id=SALE))
    db.sync.commit()
    return {"db": db, "org": org_id, "vault": vault, "log": log, "denials": denials, "cache": cache,
            "evicted": evicted, "locks": locks}


def admin_app(user: AuthenticatedUser, db: Any) -> FastAPI:
    app = FastAPI()
    app.include_router(integrations.router, prefix="/api/v1")

    async def _db() -> AsyncIterator[Any]:
        try:
            yield db
        finally:
            await db.rollback()

    async def _none() -> None:
        return None

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[org_scope_dependency] = _none
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_lxd_service] = _none
    return app


async def put(world: Dict[str, Any], body: Any, *, user: AuthenticatedUser = ADMIN,
              org_id: Optional[int] = None) -> httpx.Response:
    target = world["org"] if org_id is None else org_id
    transport = httpx.ASGITransport(app=admin_app(user, world["db"]))
    async with _HTTP(transport=transport, base_url="http://orcastra.test") as client:
        return await client.put(f"/api/v1/integrations/organizations/{target}/owner-key", json=body)


def move(key_id: str = K_NEW, reason: str = REASON) -> Dict[str, Any]:
    return {"api_key_id": key_id, "reason": reason}


def owner(world: Dict[str, Any]) -> Optional[str]:
    return world["db"].sync.execute(select(Organization.api_key_id).where(Organization.id == world["org"])).scalar_one()


def transfers(world: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [c for c in world["log"].calls if c["action"] == integration_key_ownership.TRANSFER_ACTION]


def untouched(world: Dict[str, Any], expected_owner: Optional[str] = K_OLD) -> None:
    assert owner(world) == expected_owner
    assert world["evicted"] == [] and world["cache"].cleared == []


# note: the move

async def test_an_admin_moves_an_organization_off_a_revoked_owner(world: Dict[str, Any]) -> None:
    response = await put(world, move())
    assert response.status_code == 200, response.text
    assert response.json() == {
        "organization_id": world["org"], "organization_slug": SLUG, "external_id": SELLER,
        "previous_api_key_id": K_OLD, "previous_key_state": "revoked",
        "api_key_id": K_NEW, "api_key_name": f"{K_NEW} connection", "app_name": "OrcaHub",
        "changed": True, "uncovered_cluster_ids": [], "claimed_organization_count": 1,
    }
    assert owner(world) == K_NEW
    assert world["evicted"] == [K_OLD, K_NEW]
    assert ("api_responses", "integrations:api_keys:list*") in world["cache"].cleared
    assert world["locks"] == [K_NEW], "the new owner is judged inside its ownership lock"
    (record,) = transfers(world)
    assert (record["target_type"], record["target_id"], record["result"]) == ("organization", str(world["org"]),
                                                                            "success")
    assert (record["target_organization_id"], record["target_organization_slug"]) == (world["org"], SLUG)
    assert record["details"] == {
        "organization_slug": SLUG, "external_id": SELLER, "from_key_id": K_OLD, "from_key_state": "revoked",
        "to_key_id": K_NEW, "to_key_name": f"{K_NEW} connection", "to_key_app_name": "OrcaHub",
        "reason": REASON, "actor_email": ADMIN.email, "uncovered_cluster_ids": [], "trigger": "admin",
    }
    assert (record["before_state"], record["after_state"]) == ({"owner_key_id": K_OLD}, {"owner_key_id": K_NEW})


async def test_only_key_metadata_is_read_and_never_from_the_cache(world: Dict[str, Any]) -> None:
    assert (await put(world, move())).status_code == 200
    assert world["vault"].reads, "the new owner was never read"
    assert all(at.endswith("/metadata") and use_cache is False for at, use_cache in world["vault"].reads)


def test_the_move_is_a_high_severity_admin_action() -> None:
    action = integration_key_ownership.TRANSFER_ACTION
    assert action == "integration.organization.owner.transfer"
    assert ACTION_CATEGORIES[action] is EventCategory.ADMIN and action in HIGH_SEVERITY_ACTIONS


async def test_an_alias_admin_may_move_it(world: Dict[str, Any]) -> None:
    assert (await put(world, move(), user=ALIAS_ADMIN)).status_code == 200
    assert owner(world) == K_NEW


# note: what the move does to the external routes

def push(key_id: str, members: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    return {"include_clusters": False, "include_projects": False,
            "organizations": [{"id": SELLER, "name": f"Renamed by {key_id}", "slug": SLUG}],
            "members": members or []}


async def test_the_new_owner_syncs_it_and_the_old_one_is_refused(world: Dict[str, Any]) -> None:
    assert (await put(world, move())).status_code == 200
    db = world["db"]
    refused = await send(key(K_OLD), db, push(K_OLD))
    assert refused.status_code == 200 and refused.json()["organizations_synced"] == 0
    row = db.sync.execute(select(Organization).where(Organization.id == world["org"])).scalar_one()
    assert (row.api_key_id, row.name) == (K_NEW, "Acme Cloud"), "the old owner's push changed the row"
    synced = await send(key(K_NEW), db, push(K_NEW))
    assert synced.json()["organizations_synced"] == 1
    db.sync.expire_all()
    assert db.sync.execute(select(Organization.name).where(Organization.id == world["org"])).scalar_one() == (
        f"Renamed by {K_NEW}"
    )


async def test_the_old_owner_can_no_longer_write_its_members(world: Dict[str, Any]) -> None:
    assert (await put(world, move())).status_code == 200
    member = {"email": "tenant@example.test", "name": None, "organization_id": SELLER, "role": "TENANT"}
    stale = await send(key(K_OLD), world["db"], {**push(K_OLD), "organizations": [], "members": [member]})
    assert stale.json()["members_synced"] == 0
    rows = world["db"].sync.execute(select(OrganizationMember.user_email)).scalars().all()
    assert rows == []
    fresh = await send(key(K_NEW), world["db"], {**push(K_NEW), "organizations": [], "members": [member]})
    assert fresh.json()["members_synced"] == 1


async def test_provisioning_follows_the_owner(world: Dict[str, Any]) -> None:
    assert (await put(world, move())).status_code == 200
    old = await send(key(K_OLD), world["db"], provision_body(SELLER, ["buyer@example.test"]), path=PROVISION)
    assert old.status_code == 409 and "different integration key" in old.json()["detail"]
    new = await send(key(K_NEW), world["db"], provision_body(SELLER, ["buyer@example.test"]), path=PROVISION)
    assert new.status_code == 200, new.text
    assert new.json()["organization_id"] == world["org"] and new.json()["org_created"] is False


# note: nothing to move

async def test_moving_to_the_current_owner_changes_nothing(world: Dict[str, Any]) -> None:
    response = await put(world, move(K_OLD))
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["changed"], body["previous_api_key_id"], body["previous_key_state"]) == (False, K_OLD, "revoked")
    untouched(world)
    assert transfers(world) == []


async def test_an_unowned_organization_is_assigned(world: Dict[str, Any]) -> None:
    world["db"].sync.execute(Organization.__table__.update().values(api_key_id=None))
    world["db"].sync.commit()
    # note: a Core UPDATE bypasses the identity map, and the test session keeps objects on commit
    world["db"].sync.expire_all()
    response = await put(world, move())
    assert response.status_code == 200, response.text
    assert (response.json()["previous_api_key_id"], response.json()["previous_key_state"]) == (None, "unclaimed")
    assert owner(world) == K_NEW and world["evicted"] == [K_NEW]
    (record,) = transfers(world)
    assert record["before_state"] == {"owner_key_id": None} and record["details"]["from_key_state"] == "unclaimed"


@pytest.mark.parametrize("body", [{"api_key_id": None, "reason": REASON}, {"reason": REASON}, {"api_key_id": "",
                                  "reason": REASON}], ids=["null", "absent", "empty"])
async def test_there_is_no_release(world: Dict[str, Any], body: Dict[str, Any]) -> None:
    response = await put(world, body)
    assert response.status_code == 422
    untouched(world)


@pytest.mark.parametrize("key_id", ["../oak_new_owner", "oak new", "oak_" + "a" * 125, "oak/x"])
async def test_a_key_id_outside_the_charset_is_422(world: Dict[str, Any], key_id: str) -> None:
    assert (await put(world, move(key_id))).status_code == 422
    untouched(world)


@pytest.mark.parametrize("reason", ["", "   ", "x" * 501, None], ids=["empty", "blank", "too-long", "null"])
async def test_a_reason_is_required(world: Dict[str, Any], reason: Optional[str]) -> None:
    assert (await put(world, {"api_key_id": K_NEW, "reason": reason})).status_code == 422
    untouched(world)


async def test_the_reason_is_recorded_trimmed(world: Dict[str, Any]) -> None:
    assert (await put(world, move(reason="   moved after the leak   "))).status_code == 200
    assert transfers(world)[0]["details"]["reason"] == "moved after the leak"


# note: a key that cannot own it

TARGETS = [
    ("missing", None, 409, "target_key_missing"),
    ("revoked", {"is_revoked": True}, 409, "target_key_revoked"),
    ("expired", {"expires_at": "2020-01-01T00:00:00+00:00"}, 409, "target_key_expired"),
    ("unreadable_expiry", {"expires_at": "not-a-date"}, 409, "target_key_expired"),
    ("empty_expiry", {"expires_at": ""}, 409, "target_key_expired"),
    ("inactive", {"is_active": False}, 409, "target_key_inactive"),
    ("not_orcahub", {"app_type": "mcp"}, 409, "target_key_not_orcahub"),
]


@pytest.mark.parametrize("state, status, code", [t[1:] for t in TARGETS], ids=[t[0] for t in TARGETS])
async def test_a_key_that_could_not_own_it_is_refused(
    world: Dict[str, Any], state: Optional[Dict[str, Any]], status: int, code: str
) -> None:
    if state is None:
        world["vault"].docs.pop(path(K_NEW))
    else:
        world["vault"].docs[path(K_NEW)].update(state)
    response = await put(world, move())
    assert response.status_code == status, response.text
    assert response.json()["detail"]["error"] == code and response.json()["detail"]["message"]
    untouched(world)
    (record,) = transfers(world)
    assert (record["result"], record["error_code"]) == ("denied", code)
    assert world["locks"] == [K_NEW]


async def test_a_revoke_that_lands_while_waiting_for_the_lock_is_seen(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A revoke of the new key holds the same lock. If it finishes first, the move must see it."""
    real_lock = integration_key_ownership.serialize_key_ownership

    @asynccontextmanager
    async def _revoked_meanwhile(db: Any, key_id: str) -> AsyncIterator[None]:
        world["vault"].docs[path(key_id)].update(is_revoked=True, is_active=False)
        async with real_lock(db, key_id):
            yield

    monkeypatch.setattr(integrations, "serialize_key_ownership", _revoked_meanwhile)
    response = await put(world, move())
    assert response.status_code == 409 and response.json()["detail"]["error"] == "target_key_revoked"
    untouched(world)


async def test_an_unreadable_key_store_is_503_and_moves_nothing(world: Dict[str, Any]) -> None:
    world["vault"].down = True
    response = await put(world, move())
    assert response.status_code == 503
    assert response.json()["detail"] == (
        "The API key store could not be read, so the new owner key was not checked and nothing changed. Try again."
    )
    untouched(world)
    assert [(r["result"], r["error_code"]) for r in transfers(world)] == [("denied", "target_key_unverifiable")]


async def test_a_key_without_a_type_may_own_it(world: Dict[str, Any]) -> None:
    untyped = key(K_NEW)
    untyped.pop("app_type")
    world["vault"].docs[path(K_NEW)] = untyped
    assert (await put(world, move())).status_code == 200
    assert owner(world) == K_NEW


# note: the organization

async def test_an_unknown_organization_is_404(world: Dict[str, Any]) -> None:
    response = await put(world, move(), org_id=9999)
    assert (response.status_code, response.json()["detail"]) == (404, "Organization not found")
    untouched(world)
    assert transfers(world) == []


async def test_a_dashboard_organization_has_no_owner_key(world: Dict[str, Any]) -> None:
    world["db"].sync.execute(Organization.__table__.update().values(source="local", api_key_id=None))
    world["db"].sync.commit()
    # note: a Core UPDATE bypasses the identity map, and the test session keeps objects on commit
    world["db"].sync.expire_all()
    response = await put(world, move())
    assert response.status_code == 409 and response.json()["detail"]["error"] == "organization_not_synced"
    untouched(world, expected_owner=None)
    assert [(r["result"], r["error_code"]) for r in transfers(world)] == [("denied", "organization_not_synced")]


async def test_clusters_the_new_key_does_not_name_are_reported(world: Dict[str, Any]) -> None:
    world["db"].add(OrganizationCluster(organization_id=world["org"], cluster_id="other-cluster"))
    world["db"].sync.commit()
    response = await put(world, move())
    assert response.json()["uncovered_cluster_ids"] == ["other-cluster"]
    assert transfers(world)[0]["details"]["uncovered_cluster_ids"] == ["other-cluster"]


async def test_the_count_says_when_the_new_owner_claims_more_than_one(world: Dict[str, Any]) -> None:
    world["db"].add(Organization(name="Buyer", slug="buyer-co", source="orcahub", external_id="ext-buyer",
                                 api_key_id=K_NEW, created_by="system", is_active=True))
    world["db"].sync.commit()
    assert (await put(world, move())).json()["claimed_organization_count"] == 2


# note: who may call

@pytest.mark.parametrize("user", [PARTNER, TENANT], ids=["partner", "tenant"])
async def test_a_partner_or_a_tenant_is_refused(world: Dict[str, Any], user: AuthenticatedUser) -> None:
    response = await put(world, move(), user=user)
    assert response.status_code == 403
    untouched(world)
    assert transfers(world) == [] and world["vault"].reads == []
    assert [d["action"] for d in world["denials"].calls] == ["rbac.access_denied"]


async def test_read_only_mode_refuses_first(world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    locked = dependencies.get_settings().model_copy(update={"read_only_mode": True})
    monkeypatch.setattr(dependencies, "get_settings", lambda: locked)
    response = await put(world, move())
    assert response.status_code == 403 and "read-only" in response.json()["detail"]
    untouched(world)


async def test_the_lock_is_a_postgres_advisory_lock_on_the_key() -> None:
    """The seam the handler takes. On PostgreSQL it is a transaction advisory lock keyed by the key id."""
    executed: List[Any] = []

    class _Bind:
        class dialect:  # noqa: N801 - the attribute shape SQLAlchemy exposes
            name = "postgresql"

    class _Session:
        def get_bind(self) -> Any:
            return _Bind()

        async def execute(self, statement: Any, params: Any = None) -> None:
            executed.append((str(statement), params))

    async with integration_key_ownership.serialize_key_ownership(_Session(), K_NEW):
        pass
    ((sql, params),) = executed
    assert sql == "SELECT pg_advisory_xact_lock(:lock_key)"
    assert params == {"lock_key": integration_key_ownership._advisory_key(K_NEW)}
    assert integration_key_ownership._advisory_key(K_NEW) != integration_key_ownership._advisory_key(K_OLD)
