"""An integration key's secret is rotated in place: same key id, new secret, the old one dead at once.

Rotating by minting a new key left every organization the old key synced refusing the new one
(a key may only update the orgs it owns), so org and member sync stopped. These run the real
rotate handler and the real key validation against an in-memory Vault and cache.
"""

from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

from app.core.auth import AuthenticatedUser
from app.routers import integrations

KEY = "oak_" + "a" * 32
BASE = f"secret/data/{integrations.VAULT_INTEGRATIONS_PATH}/api_keys/{KEY}"
OWNER = AuthenticatedUser(user_id="p", email="partner@example.test", groups=["role_partner"])


class _Vault:
    def __init__(self, docs: Dict[str, Dict[str, Any]]) -> None:
        self.docs = docs
        self.writes: List[str] = []

    def vault_read(self, path: str) -> Dict[str, Any]:
        if path not in self.docs:
            raise HTTPException(status_code=404, detail="not found")
        return dict(self.docs[path])

    def vault_kv_put(self, path: str, data: Dict[str, Any]) -> None:
        self.writes.append(path)
        self.docs[path] = dict(data)


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
def world(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "unit-test-secret")
    old_secret, old_hash = integrations.generate_api_key_secret()
    metadata = {"id": KEY, "name": "conn", "app_name": "hub", "app_type": "orcahub", "is_active": True,
                "is_revoked": False, "created_at": "2026-09-01T00:00:00", "created_by": OWNER.email,
                "cluster_permissions": [{"cluster_id": "c1", "permissions": ["read"], "projects": ["*"]}]}
    vault = _Vault({f"{BASE}/metadata": metadata, f"{BASE}/secret": {"hash": old_hash, "raw": old_secret}})
    cache = _Cache()
    events: List[Dict[str, Any]] = []

    async def authorize(_key: str, _user: Any, _db: Any) -> Dict[str, Any]:
        return dict(metadata)

    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: vault)
    monkeypatch.setattr(integrations, "get_cache_manager", lambda: cache)
    monkeypatch.setattr(integrations, "_authorize_api_key", authorize)
    monkeypatch.setattr(integrations, "get_user_mappings_from_vault", lambda _v, _k: [])
    monkeypatch.setattr(integrations, "_schedule_last_used_update", lambda _k: None)
    monkeypatch.setattr(integrations.activity_logger, "log_activity", lambda **kw: events.append(kw) or "id")
    integrations._key_cache.clear()
    yield {"old": old_secret, "vault": vault, "metadata": metadata, "events": events}
    integrations._key_cache.clear()


async def _valid(secret: str) -> bool:
    try:
        await integrations.validate_api_key(KEY, secret)
        return True
    except HTTPException as exc:
        assert exc.status_code == 401
        return False


async def test_rotation_keeps_the_id_and_kills_the_old_secret(world) -> None:
    assert await _valid(world["old"]), "precondition: the old secret works and is now cached"
    answer = await integrations.rotate_api_key_secret(KEY, None, OWNER, None)
    assert answer.api_key_id == KEY and answer.api_key.id == KEY
    assert answer.api_key_secret != world["old"] and answer.api_key_secret.startswith("oas_")
    assert not await _valid(world["old"]), "the cached old secret must stop working at once"
    assert await _valid(answer.api_key_secret)
    stored = world["vault"].docs[f"{BASE}/secret"]
    assert stored["raw"] == answer.api_key_secret, "callbacks sign with the new secret"
    assert integrations.verify_api_key_secret(answer.api_key_secret, stored["hash"])
    assert world["vault"].docs[f"{BASE}/metadata"] == world["metadata"], "the key itself is unchanged"
    assert world["vault"].writes == [f"{BASE}/secret"]


async def test_another_workers_cached_copy_of_the_old_secret_is_dropped(world) -> None:
    await integrations.rotate_api_key_secret(KEY, None, OWNER, None)
    # another worker still holds the old secret's entry in its own process cache
    integrations._store_key_metadata(integrations._api_key_digest(KEY, world["old"]), KEY, dict(world["metadata"]))
    assert not await _valid(world["old"]), "the shared tombstone must override a stale local hit"


@pytest.mark.parametrize("state", [{"is_revoked": True}, {"is_active": False}, {"expires_at": "2020-01-01T00:00:00"}])
async def test_an_unusable_key_is_not_rotated(world, state: Dict[str, Any]) -> None:
    world["metadata"].update(state)
    world["vault"].docs[f"{BASE}/metadata"].update(state)
    with pytest.raises(HTTPException) as caught:
        await integrations.rotate_api_key_secret(KEY, None, OWNER, None)
    assert caught.value.status_code == 409
    assert world["vault"].writes == []


async def test_rotation_is_audited_without_the_secret(world) -> None:
    answer = await integrations.rotate_api_key_secret(KEY, None, OWNER, None)
    rotations = [e for e in world["events"] if e["action"] == integrations.API_KEY_ROTATE]
    assert len(rotations) == 1 and rotations[0]["target_id"] == KEY
    assert answer.api_key_secret not in repr(rotations[0]) and world["old"] not in repr(rotations[0])


def test_the_settings_page_offers_the_rotation() -> None:
    from pathlib import Path

    frontend = Path(__file__).resolve().parents[2] / "frontend" / "src"
    button = (frontend / "components" / "integrations" / "rotate-secret-button.tsx").read_text()
    assert "api.integrations.rotateAPIKeySecret(keyId)" in button
    assert "if (inFlight.current) return;" in button, "two rotations in one tick would show a dead secret"
    client = (frontend / "lib" / "api.ts").read_text()
    call = client[client.index("rotateAPIKeySecret:") :]
    assert "/rotate-secret`" in call[:300] and 'method: "POST"' in call[:300]
    page = (frontend / "components" / "integrations-settings.tsx").read_text()
    assert "<RotateSecretButton" in page and 'title="Secret Rotated"' in page
    assert "isRotatable(apiKey) && (" in page, "a revoked, inactive or expired key must not offer a rotation that fails"
    assert "[...queue, response]" in page and "queue.slice(1)" in page, "overlapping rotations must each show their secret"
    assert "if (!key.expires_at) return true;" in button and "`${key.expires_at}Z`" in button, "a naive expiry is UTC"


async def test_a_key_whose_grants_no_longer_validate_is_not_rotated(world) -> None:
    """The response is built before the write, so a stored grant that fails today's model loses nothing."""
    bad = [{"cluster_id": "c1", "permissions": [], "projects": ["*"]}]
    world["metadata"]["cluster_permissions"] = bad
    world["vault"].docs[f"{BASE}/metadata"]["cluster_permissions"] = bad
    with pytest.raises(HTTPException) as caught:
        await integrations.rotate_api_key_secret(KEY, None, OWNER, None)
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
        await integrations.rotate_api_key_secret(KEY, None, OWNER, None)
    assert caught.value.status_code == 500 and "Rotate again" in caught.value.detail
    assert not await _valid(world["old"]), "a cached copy of the old secret outlived a rotation that landed"


def test_the_tombstone_outlives_a_validation_still_in_flight(world) -> None:
    ttls: List[int] = []
    cache = integrations.get_cache_manager()
    original = cache.set
    cache.set = lambda ns, key, value, ttl=0: ttls.append(ttl) or original(ns, key, value, ttl)
    integrations.evict_api_key_cache(KEY)
    assert ttls and ttls[0] >= integrations.API_KEY_CACHE_TTL + integrations.VALIDATE_KEY_TIMEOUT


def test_the_raw_backfill_never_writes_over_a_rotation(world, monkeypatch) -> None:
    """A legacy key without a raw copy, validated with the old secret while a rotation lands."""
    old_hash = world["vault"].docs[f"{BASE}/secret"]["hash"]
    _, new_hash = integrations.generate_api_key_secret()
    world["vault"].docs[f"{BASE}/secret"] = {"hash": new_hash}  # rotated, as a legacy doc would look mid-race
    monkeypatch.setattr(integrations, "get_api_key_from_vault", lambda _v, _k: (dict(world["metadata"]), old_hash))
    integrations._validate_api_key_blocking(KEY, world["old"])
    assert world["vault"].writes == [], "the backfill wrote the old secret over the rotated hash"


@pytest.fixture
def partner_scope():
    from app.core import org_scope

    token = org_scope._allowed_clusters_var.set({"c1"})
    yield
    org_scope._allowed_clusters_var.reset(token)


OTHER = AuthenticatedUser(user_id="q", email="teammate@example.test", groups=["role_partner"])


async def test_a_partner_cannot_take_the_secret_of_a_key_reaching_past_their_scope(world, partner_scope) -> None:
    """An admin-minted key granting the partner's cluster and one they cannot reach."""
    grants = [{"cluster_id": "c1", "permissions": ["read"], "projects": ["*"]},
              {"cluster_id": "c9", "permissions": ["read", "write"], "projects": ["*"]}]
    for doc in (world["metadata"], world["vault"].docs[f"{BASE}/metadata"]):
        doc.update({"created_by": "ops@example.test", "cluster_permissions": grants})
    with pytest.raises(HTTPException) as caught:
        await integrations.rotate_api_key_secret(KEY, None, OTHER, None)
    assert caught.value.status_code == 403 and "c9" in caught.value.detail
    assert world["vault"].writes == [] and await _valid(world["old"])


async def test_a_non_creator_rotation_is_held_to_the_grant_bound(world, partner_scope, monkeypatch) -> None:
    seen: List[str] = []

    async def bound(_db: Any, email: str, grants: Any, *_a: Any) -> None:
        seen.append(email)
        raise HTTPException(status_code=403, detail="Not permitted to grant these projects")

    monkeypatch.setattr(integrations, "_refuse_a_grant_past_the_project_bound", bound)
    for doc in (world["metadata"], world["vault"].docs[f"{BASE}/metadata"]):
        doc["created_by"] = "ops@example.test"
    with pytest.raises(HTTPException) as caught:
        await integrations.rotate_api_key_secret(KEY, None, OTHER, None)
    assert caught.value.status_code == 403 and seen == [OTHER.email]
    assert world["vault"].writes == []


async def test_the_creator_and_an_admin_rotate_without_the_mint_check(world, partner_scope, monkeypatch) -> None:
    async def refuse(*_a: Any, **_k: Any) -> None:
        raise AssertionError("the mint check ran for a caller who already held the secret")

    monkeypatch.setattr(integrations, "_refuse_a_grant_past_the_project_bound", refuse)
    answer = await integrations.rotate_api_key_secret(KEY, None, OWNER, None)
    assert answer.api_key_id == KEY
    from app.core import org_scope

    token = org_scope._allowed_clusters_var.set(None)
    try:
        admin = AuthenticatedUser(user_id="a", email="ops@example.test", groups=["role_admin"])
        assert (await integrations.rotate_api_key_secret(KEY, None, admin, None)).api_key_id == KEY
    finally:
        org_scope._allowed_clusters_var.reset(token)
