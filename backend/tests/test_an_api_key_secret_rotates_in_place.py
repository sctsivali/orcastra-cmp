"""An integration key's secret is rotated in place: same key id, new secret, the old one dead at once.

Rotating by minting a new key left every organization the old key synced refusing the new one
(a key may only update the orgs it owns), so org and member sync stopped. These run the real
rotate handler and the real key validation against an in-memory Vault and cache.

A rotation hands its caller a working secret, unlike a revoke or a delete. So a Partner who did not
mint the key rotates it only when every cluster it grants is inside their scope and every project
inside their bound; its creator and an Admin skip that check. An add-on credential is refused: its
installation rotates it.
"""

from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core.auth import AuthenticatedUser
from app.models.db_models import Organization
from app.routers import integrations
from app.routers.integrations import SyncDataRequest
from app.services.add_on_credential_edits import ROTATE_DETAIL
from tests._sqlite_session import org_tables, sqlite_session
from tests.test_a_capability_grant_leaves_a_record import _RecordingLogger

KEY = "oak_" + "a" * 32
BASE = f"secret/data/{integrations.VAULT_INTEGRATIONS_PATH}/api_keys/{KEY}"
OWNER = AuthenticatedUser(user_id="p", email="partner@example.test", groups=["role_partner"])
OTHER = AuthenticatedUser(user_id="q", email="teammate@example.test", groups=["role_partner"])
ADMIN = AuthenticatedUser(user_id="a", email="ops@example.test", groups=["role_admin"])
ALIAS_ADMIN = AuthenticatedUser(user_id="a2", email="alias@example.test", groups=["authentik Admins"])


class _Vault:
    def __init__(self, docs: Dict[str, Dict[str, Any]]) -> None:
        self.docs = docs
        self.writes: List[str] = []

    def vault_read(self, path: str, use_cache: bool = True) -> Dict[str, Any]:
        if path not in self.docs:
            raise HTTPException(status_code=404, detail="not found")
        return dict(self.docs[path])

    def vault_kv_put(self, path: str, data: Dict[str, Any]) -> None:
        self.writes.append(path)
        self.docs[path] = dict(data)


class _Cache:
    def __init__(self) -> None:
        self.store: Dict[tuple, Any] = {}
        self.cleared: List[tuple] = []

    def get(self, ns: str, key: str) -> Any:
        return self.store.get((ns, key))

    def set(self, ns: str, key: str, value: Any, ttl: int = 0) -> None:
        self.store[(ns, key)] = value

    def delete(self, *args: Any, **_k: Any) -> None:
        self.cleared.append(args)

    def delete_pattern(self, *args: Any, **_k: Any) -> None:
        self.cleared.append(args)


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "unit-test-secret")
    old_secret, old_hash = integrations.generate_api_key_secret()
    metadata = {"id": KEY, "name": "conn", "app_name": "hub", "app_type": "orcahub", "is_active": True,
                "is_revoked": False, "created_at": "2026-09-01T00:00:00", "created_by": OWNER.email,
                "capabilities": ["inventory.read", "org.sync", "tenant.provision"],
                "cluster_permissions": [{"cluster_id": "c1", "permissions": ["read"], "projects": ["*"]}]}
    vault = _Vault({f"{BASE}/metadata": metadata, f"{BASE}/secret": {"hash": old_hash, "raw": old_secret}})
    cache = _Cache()
    log = _RecordingLogger()

    async def authorize(_key: str, _user: Any, _db: Any) -> Dict[str, Any]:
        return dict(metadata)

    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: vault)
    monkeypatch.setattr(integrations, "get_cache_manager", lambda: cache)
    monkeypatch.setattr(integrations, "_authorize_api_key", authorize)
    monkeypatch.setattr(integrations, "get_user_mappings_from_vault", lambda _v, _k: [])
    monkeypatch.setattr(integrations, "_schedule_last_used_update", lambda _k: None)
    monkeypatch.setattr(integrations, "activity_logger", log)
    integrations._key_cache.clear()
    yield {"old": old_secret, "vault": vault, "metadata": metadata, "log": log, "cache": cache}
    integrations._key_cache.clear()


def _stored(world: Dict[str, Any], **changes: Any) -> None:
    """Change the key as Vault holds it and as the authorization read returns it."""
    world["metadata"].update(changes)
    world["vault"].docs[f"{BASE}/metadata"].update(changes)


async def _valid(secret: str) -> bool:
    try:
        await integrations.validate_api_key(KEY, secret)
        return True
    except HTTPException as exc:
        assert exc.status_code == 401
        return False


async def _rotate(user: AuthenticatedUser = OWNER) -> Any:
    return await integrations.rotate_api_key_secret(KEY, None, user, None)


def _rotations(world: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [c for c in world["log"].calls if c["action"] == integrations.API_KEY_ROTATE]


async def test_rotation_keeps_the_id_and_kills_the_old_secret(world) -> None:
    assert await _valid(world["old"]), "precondition: the old secret works and is now cached"
    answer = await _rotate()
    assert answer.api_key_id == KEY and answer.api_key.id == KEY
    assert answer.api_key_secret != world["old"] and answer.api_key_secret.startswith("oas_")
    assert not await _valid(world["old"]), "the cached old secret must stop working at once"
    assert await _valid(answer.api_key_secret)
    stored = world["vault"].docs[f"{BASE}/secret"]
    assert stored["raw"] == answer.api_key_secret, "callbacks sign with the new secret"
    assert integrations.verify_api_key_secret(answer.api_key_secret, stored["hash"])
    assert world["vault"].docs[f"{BASE}/metadata"] == world["metadata"], "the key itself is unchanged"
    assert world["vault"].writes == [f"{BASE}/secret"]
    assert ("api_responses", "integrations:api_keys:list*") in world["cache"].cleared


async def test_another_workers_cached_copy_of_the_old_secret_is_dropped(world) -> None:
    await _rotate()
    # another worker still holds the old secret's entry in its own process cache
    integrations._store_key_metadata(integrations._api_key_digest(KEY, world["old"]), KEY, dict(world["metadata"]))
    assert not await _valid(world["old"]), "the shared tombstone must override a stale local hit"


@pytest.mark.parametrize("state", [{"is_revoked": True}, {"is_active": False}, {"expires_at": "2020-01-01T00:00:00"},
                                   {"expires_at": "not-a-date"}],
                         ids=["revoked", "inactive", "expired", "unreadable-expiry"])
async def test_an_unusable_key_is_not_rotated(world, state: Dict[str, Any]) -> None:
    _stored(world, **state)
    with pytest.raises(HTTPException) as caught:
        await _rotate()
    assert caught.value.status_code == 409
    assert world["vault"].writes == []


async def test_an_add_on_credential_is_rotated_by_its_installation(world) -> None:
    _stored(world, extension_install_id=7, app_type="automation")
    for user in (OWNER, ADMIN):
        with pytest.raises(HTTPException) as caught:
            await _rotate(user)
        assert (caught.value.status_code, caught.value.detail) == (409, ROTATE_DETAIL)
    assert "rotate it from its installation" in ROTATE_DETAIL
    assert world["vault"].writes == [] and _rotations(world) == []


async def test_rotation_is_audited_without_the_secret(world) -> None:
    answer = await _rotate()
    (record,) = _rotations(world)
    assert record["target_id"] == KEY and record["target_type"] == "api_key" and record["result"] == "success"
    assert answer.api_key_secret not in repr(record) and world["old"] not in repr(record)


def test_a_rotation_is_filed_with_its_siblings() -> None:
    from app.services.activity_logger import ACTION_CATEGORIES, HIGH_SEVERITY_ACTIONS, EventCategory

    assert integrations.API_KEY_ROTATE == "integration.api_key.secret.rotate"
    assert ACTION_CATEGORIES[integrations.API_KEY_ROTATE] is EventCategory.ADMIN
    assert ACTION_CATEGORIES[integrations.API_KEY_ROTATE] == ACTION_CATEGORIES["integration.api_key.revoke"]
    assert integrations.API_KEY_ROTATE in HIGH_SEVERITY_ACTIONS


async def test_a_key_whose_stored_record_no_longer_validates_is_not_rotated(world) -> None:
    """The response is built before the write, so a stored record today's model refuses loses nothing."""
    _stored(world, cluster_permissions=[{"permissions": ["read"], "projects": ["*"]}])
    with pytest.raises(HTTPException) as caught:
        await _rotate()
    assert caught.value.status_code == 409
    assert world["vault"].writes == [], "the old secret must survive a rotation that cannot answer"
    assert await _valid(world["old"])


async def test_a_write_whose_reply_is_lost_still_drops_every_cached_copy(world, monkeypatch) -> None:
    assert await _valid(world["old"])

    def lost(path: str, data: Dict[str, Any]) -> None:
        world["vault"].docs[path] = dict(data)  # the write lands, then its reply is lost
        raise RuntimeError("read timed out")

    monkeypatch.setattr(world["vault"], "vault_kv_put", lost)
    with pytest.raises(HTTPException) as caught:
        await _rotate()
    assert caught.value.status_code == 503
    assert caught.value.detail == "The secret rotation did not confirm. Rotate again before using this key."
    assert not await _valid(world["old"]), "a cached copy of the old secret outlived a rotation that landed"


def test_the_tombstone_outlives_a_validation_still_in_flight(world) -> None:
    integrations.evict_api_key_cache(KEY)
    assert world["cache"].store[(integrations._REVOKED_NS, KEY)] is True
    ttls: List[int] = []
    cache = integrations.get_cache_manager()
    original = cache.set
    cache.set = lambda ns, key, value, ttl=0: ttls.append(ttl) or original(ns, key, value, ttl)
    integrations.evict_api_key_cache(KEY)
    assert ttls and ttls[0] >= integrations.API_KEY_CACHE_TTL + integrations.VALIDATE_KEY_TIMEOUT


def _legacy_validation(world: Dict[str, Any], monkeypatch, verified_hash: str) -> None:
    """A key with a callback base whose secret document has no raw copy yet."""
    metadata = {**world["metadata"], "app_url": "https://hub.example.test"}
    monkeypatch.setattr(integrations, "get_api_key_from_vault", lambda _v, _k: (dict(metadata), verified_hash))
    integrations._validate_api_key_blocking(KEY, world["old"])


def test_the_raw_backfill_never_writes_over_a_rotation(world, monkeypatch) -> None:
    """Validated with the old secret while a rotation lands between the two reads."""
    old_hash = world["vault"].docs[f"{BASE}/secret"]["hash"]
    _, new_hash = integrations.generate_api_key_secret()
    world["vault"].docs[f"{BASE}/secret"] = {"hash": new_hash}  # rotated, as a legacy doc would look mid-race
    _legacy_validation(world, monkeypatch, old_hash)
    assert world["vault"].writes == [], "the backfill wrote the old secret over the rotated hash"


def test_the_raw_backfill_still_fills_the_hash_it_verified(world, monkeypatch) -> None:
    """The guard narrows the backfill, it does not remove it."""
    old_hash = world["vault"].docs[f"{BASE}/secret"]["hash"]
    world["vault"].docs[f"{BASE}/secret"] = {"hash": old_hash}
    _legacy_validation(world, monkeypatch, old_hash)
    assert world["vault"].docs[f"{BASE}/secret"] == {"hash": old_hash, "raw": world["old"]}


# note: who may rotate. Managing a key needs its side (_authorize_api_key), and a rotation hands
# over a working secret, so a Partner who did not mint the key is also held to the key's reach.

@pytest.fixture
def partner_scope():
    from app.core import org_scope

    token = org_scope._allowed_clusters_var.set({"c1"})
    yield
    org_scope._allowed_clusters_var.reset(token)


def _denied(world: Dict[str, Any]) -> List[tuple]:
    return [(r.get("result"), r.get("error_code")) for r in _rotations(world)]


async def test_a_partner_cannot_take_the_secret_of_a_key_reaching_past_their_scope(world, partner_scope) -> None:
    """An admin-minted key granting the partner's cluster and one they cannot reach."""
    _stored(world, created_by="ops@example.test",
            cluster_permissions=[{"cluster_id": "c1", "permissions": ["read"], "projects": ["*"]},
                                 {"cluster_id": "c9", "permissions": ["read", "write"], "projects": ["*"]}])
    with pytest.raises(HTTPException) as caught:
        await _rotate(OTHER)
    assert caught.value.status_code == 403 and "c9" in caught.value.detail
    assert world["vault"].writes == [] and await _valid(world["old"])
    assert _denied(world) == [("denied", "cluster_out_of_scope")], "a refused rotation left no audit record"


async def test_a_non_creator_rotation_is_held_to_the_grant_bound(world, partner_scope, monkeypatch) -> None:
    seen: List[str] = []

    async def bound(_db: Any, email: str, grants: Any, *_a: Any) -> None:
        seen.append(email)
        raise HTTPException(status_code=403, detail="Not permitted to grant these projects")

    monkeypatch.setattr(integrations, "_refuse_a_grant_past_the_project_bound", bound)
    _stored(world, created_by="ops@example.test")
    with pytest.raises(HTTPException) as caught:
        await _rotate(OTHER)
    assert caught.value.status_code == 403 and seen == [OTHER.email]
    assert world["vault"].writes == []
    assert _denied(world) == [("denied", "project_out_of_bound")], "a refused rotation left no audit record"


async def test_the_bound_refuses_a_project_another_party_holds(world, partner_scope, monkeypatch) -> None:
    """The real bound: c1 is held by another party too, and the key names a project the partner cannot see."""
    from app.core import org_scope

    async def held(_db: Any, _email: str, clusters: set) -> set:
        return set(clusters)

    monkeypatch.setattr(integrations, "_clusters_held_by_another_party", held)
    _stored(world, created_by="ops@example.test",
            cluster_permissions=[{"cluster_id": "c1", "permissions": ["read"], "projects": ["their-shop"]}])
    token = org_scope._visible_projects_var.set({"c1": {"default", "my-shop"}})
    try:
        with pytest.raises(HTTPException) as caught:
            await _rotate(OTHER)
    finally:
        org_scope._visible_projects_var.reset(token)
    assert caught.value.status_code == 403 and "c1/their-shop" in caught.value.detail
    assert world["vault"].writes == []
    assert _denied(world) == [("denied", "project_out_of_bound")]


async def test_a_non_creator_on_their_own_sides_cluster_may_rotate(world, partner_scope, monkeypatch) -> None:
    """A teammate rotating the connection key of a cluster only their side holds."""
    async def nobody_else(_db: Any, _email: str, _clusters: set) -> set:
        return set()

    monkeypatch.setattr(integrations, "_clusters_held_by_another_party", nobody_else)
    _stored(world, created_by="teammate-who-left@example.test")
    answer = await _rotate(OTHER)
    assert answer.api_key_id == KEY and await _valid(answer.api_key_secret)
    assert _denied(world) == [("success", None)]


async def test_the_creator_and_an_admin_rotate_without_the_mint_check(world, partner_scope, monkeypatch) -> None:
    async def refuse(*_a: Any, **_k: Any) -> None:
        raise AssertionError("the mint check ran for a caller who already held the secret")

    monkeypatch.setattr(integrations, "_refuse_a_grant_past_the_project_bound", refuse)
    _stored(world, created_by=OWNER.email.upper())
    assert (await _rotate(OWNER)).api_key_id == KEY, "the creator is compared without case"
    from app.core import org_scope

    token = org_scope._allowed_clusters_var.set(None)
    try:
        for admin in (ADMIN, ALIAS_ADMIN):
            assert (await _rotate(admin)).api_key_id == KEY
    finally:
        org_scope._allowed_clusters_var.reset(token)


# note: the organizations the key owns stay with it, which is the reason rotation exists

async def test_rotation_keeps_the_id_and_the_organizations_it_owns(world) -> None:
    db = sqlite_session(*org_tables())
    db.add(Organization(name="Own", slug="own", source="orcahub", external_id="ext-own", api_key_id=KEY,
                        created_by="system", is_active=True))
    await db.commit()
    answer = await _rotate()
    assert not await _valid(world["old"])
    validated = await integrations.validate_api_key(KEY, answer.api_key_secret)
    body = {"include_clusters": False, "include_projects": False,
            "organizations": [{"id": "ext-own", "name": "Own Renamed", "slug": "own"}]}
    synced = await integrations.sync_data(SyncDataRequest.model_validate(body), validated, None, db)
    assert synced.organizations_synced == 1
    org = db.sync.query(Organization).one()
    assert (org.api_key_id, org.name) == (KEY, "Own Renamed")


# note: the route's own gates

def _route(user: AuthenticatedUser) -> TestClient:
    from app.core.rbac import get_current_user

    app = FastAPI()
    app.include_router(integrations.router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[integrations.org_scope_dependency] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def test_a_tenant_cannot_rotate(world) -> None:
    tenant = AuthenticatedUser(user_id="t", email="tenant@example.test", groups=["role_tenant"])
    response = _route(tenant).post(f"/integrations/api-keys/{KEY}/rotate-secret")
    assert response.status_code == 403
    assert world["vault"].writes == []


def test_read_only_mode_refuses_a_rotation(world, monkeypatch) -> None:
    from app.core import dependencies

    locked = dependencies.get_settings().model_copy(update={"read_only_mode": True})
    monkeypatch.setattr(dependencies, "get_settings", lambda: locked)
    response = _route(OWNER).post(f"/integrations/api-keys/{KEY}/rotate-secret")
    assert response.status_code == 403 and "read-only" in response.json()["detail"]
    assert world["vault"].writes == []


def test_the_route_answers_the_secret_once(world) -> None:
    response = _route(OWNER).post(f"/integrations/api-keys/{KEY}/rotate-secret")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["api_key_id"] == KEY and body["api_key"]["id"] == KEY
    assert body["api_key_secret"] == world["vault"].docs[f"{BASE}/secret"]["raw"]


def test_the_settings_page_offers_the_rotation() -> None:
    frontend = Path(__file__).resolve().parents[2] / "frontend" / "src"
    button = (frontend / "components" / "integrations" / "rotate-secret-button.tsx").read_text()
    assert "api.integrations.rotateAPIKeySecret(keyId)" in button
    assert "if (rotateInFlightRef.current) return;" in button, "two rotations in one tick would show a dead secret"
    assert 'reconcileMutationImpact(queryClient, "integration.write")' in button
    client = (frontend / "lib" / "api.ts").read_text()
    call = client[client.index("rotateAPIKeySecret:") :]
    assert "/rotate-secret`" in call[:300] and 'method: "POST"' in call[:300]
    page = (frontend / "components" / "integrations-settings.tsx").read_text()
    assert "<RotateSecretButton" in page and 'title="Secret Rotated"' in page
    assert "const keyStatus = getAPIKeyStatus(apiKey);" in page
    assert 'keyStatus === "Active" && !apiKey.is_add_on_credential && (' in page, (
        "a key the server refuses to rotate must not offer the control"
    )
    assert "[...queue, response]" in page and "queue.slice(1)" in page, "overlapping rotations must each show their secret"
