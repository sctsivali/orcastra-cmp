"""The key routes refuse to change an add-on credential's grant, and keep its kill switch.

An add-on's credential is an integration key, so PUT /api-keys/{id}/capabilities and
/permissions reached it for any partner whose clusters overlapped its grant. That changed what a
third party holds outside the operator's install consent. The installation owns the grant now:
those edits answer 409 and name rescope. Revoke stays, and the installation's own flows (rescope,
rotation, the reconcile sweep) keep working after one. A delete is refused only while an
installation claims the key, so a superseded or reclaimed credential is deletable as before.
"""

from typing import Any, Dict, List, Optional, Set, Tuple

import pytest
from fastapi import HTTPException

from app.routers import integrations
from sqlalchemy import text

from app.services import add_on_credential_edits, extension_install
from app.services.add_on_credential_edits import CLAIM_UNKNOWN_DETAIL, DELETE_DETAIL, GRANT_EDIT_DETAIL
from app.services.extension_credentials import build_install_metadata, rotate_install_credential
from tests._sqlite_session import AsyncSessionFacade, org_tables, sqlite_session
from tests.test_a_capability_grant_leaves_a_record import PARTNER, SECRETS, _FakeVault, _RecordingLogger

ADDON = "oak_addon"
PLAIN = "oak_plain"


class Vault(_FakeVault):
    """_FakeVault, plus deletes recorded and applied like the real Vault client."""

    def __init__(self, metadata: Dict[str, Any]) -> None:
        super().__init__(metadata)
        self.deletes: List[str] = []

    def vault_read(self, path: str) -> Dict[str, Any]:
        if path not in self.store:
            raise RuntimeError(f"404 {path}")
        return dict(self.store[path])

    def vault_delete(self, path: str) -> None:
        self.deletes.append(path)
        self.store.pop(path, None)


class Cache:
    def __init__(self) -> None:
        self.cleared: List[Tuple[Any, ...]] = []

    def delete_pattern(self, *args: Any) -> None:
        self.cleared.append(args)

    def delete(self, *args: Any) -> None:
        self.cleared.append(args)

    def set(self, *args: Any, **kwargs: Any) -> None:
        self.cleared.append(("set", *args))


def addon_metadata(**extra: Any) -> Dict[str, Any]:
    metadata = build_install_metadata(
        key_id=ADDON, extension_slug="backup-bot", extension_name="Backup Bot", install_id=7,
        organization_id=1, organization_slug="acme", capabilities=["inventory.read", "lxd.proxy"],
        cluster_permissions=[{"cluster_id": "nuc1", "permissions": ["read"]}],
        created_by=PARTNER.email, expire_days=30,
    )
    return {**metadata, **extra}


def plain_metadata() -> Dict[str, Any]:
    return {
        "id": PLAIN, "name": "k", "app_name": "OrcaHub", "app_type": "orcahub",
        "capabilities": ["inventory.read"], "created_by": PARTNER.email,
        "created_at": "2026-09-01T00:00:00+00:00", "expires_at": None, "is_active": True,
        "is_revoked": False, "last_used_at": None,
        "cluster_permissions": [{"cluster_id": "nuc1", "permissions": ["read"]}],
    }


@pytest.fixture
def surface(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """Vault, the list cache, the audit log and the cluster read each record what they were asked."""
    holder: Dict[str, Any] = {"log": _RecordingLogger(), "cache": Cache(), "evicted": [], "reached": []}
    monkeypatch.setattr(integrations, "activity_logger", holder["log"])
    monkeypatch.setattr(integrations, "get_cache_manager", lambda: holder["cache"])
    monkeypatch.setattr(integrations, "get_user_mappings_from_vault", lambda *_a: [])
    monkeypatch.setattr(integrations, "evict_api_key_cache", holder["evicted"].append)
    monkeypatch.setattr(integrations, "get_current_allowed_clusters", lambda: {"nuc1"})

    async def _reach(_lxd: Any, permissions: Any) -> None:
        holder["reached"].append(permissions)

    monkeypatch.setattr(integrations, "_assert_granted_projects_exist", _reach)

    def use(metadata: Dict[str, Any]) -> Vault:
        vault = Vault(metadata)
        monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: vault)
        holder["vault"] = vault
        return vault

    holder["use"] = use
    return holder


def metadata_of(vault: Vault, key_id: str) -> Dict[str, Any]:
    return vault.store[_FakeVault._path(key_id, "metadata")]


def installations(*claims: Tuple[str, Optional[str]]) -> AsyncSessionFacade:
    """Real rows of the two columns the claim reads, one (api_key_id, previous_api_key_id) each.

    note: the full model has Postgres-only columns, so SQLite holds only the ones the query names.
    The organization tables are there too, empty, because revoke and delete first read which
    organizations the key owns (services/integration_key_ownership.py).
    """
    db = sqlite_session(*org_tables())
    db.sync.execute(text(
        "CREATE TABLE extension_installations "
        "(id INTEGER PRIMARY KEY, api_key_id TEXT NOT NULL UNIQUE, previous_api_key_id TEXT)"
    ))
    for current, previous in claims:
        db.sync.execute(
            text("INSERT INTO extension_installations (api_key_id, previous_api_key_id) VALUES (:c, :p)"),
            {"c": current, "p": previous},
        )
    db.sync.commit()
    return db


async def delete(key_id: str, db: Any) -> Any:
    return await integrations.delete_api_key(key_id, None, PARTNER, db)


async def edit_capabilities(key_id: str, capabilities: List[str]) -> Any:
    return await integrations.update_api_key_capabilities(
        key_id, integrations.APIKeyCapabilitiesRequest(capabilities=capabilities), None, PARTNER
    )


async def edit_permissions(key_id: str, clusters: List[str]) -> Any:
    grants = [integrations.ClusterPermissionRequest(cluster_id=c, permissions=["read"]) for c in clusters]
    return await integrations.update_api_key_permissions(key_id, grants, None, PARTNER, None)


# ── refused ────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("capabilities", [["inventory.read", "lxd.proxy", "org.sync"], ["inventory.read"]])
async def test_a_capability_edit_of_an_add_on_credential_is_refused(
    surface: Dict[str, Any], capabilities: List[str]
) -> None:
    """Widening and narrowing alike: rescope is the one door, so the row and the key agree."""
    vault = surface["use"](addon_metadata())
    with pytest.raises(HTTPException) as refused:
        await edit_capabilities(ADDON, capabilities)
    assert refused.value.status_code == 409
    assert refused.value.detail == GRANT_EDIT_DETAIL and "rescoping" in refused.value.detail
    assert vault.writes == [] and metadata_of(vault, ADDON)["capabilities"] == ["inventory.read", "lxd.proxy"]
    assert surface["log"].calls == [] and surface["evicted"] == [] and surface["cache"].cleared == []


async def test_a_permission_edit_of_an_add_on_credential_is_refused_before_any_cluster_is_read(
    surface: Dict[str, Any],
) -> None:
    vault = surface["use"](addon_metadata())
    with pytest.raises(HTTPException) as refused:
        await edit_permissions(ADDON, ["nuc1"])
    assert refused.value.status_code == 409 and refused.value.detail == GRANT_EDIT_DETAIL
    assert surface["reached"] == []
    assert vault.writes == []
    assert metadata_of(vault, ADDON)["cluster_permissions"] == [{"cluster_id": "nuc1", "permissions": ["read"]}]
    assert surface["evicted"] == [] and surface["cache"].cleared == []


@pytest.mark.parametrize("claim", [(ADDON, None), ("oak_next", ADDON)], ids=["current", "previous"])
async def test_deleting_an_add_on_credential_an_installation_claims_is_refused(
    surface: Dict[str, Any], claim: Tuple[str, Optional[str]]
) -> None:
    """The live key, and the previous one a rotation's grace window still honours."""
    vault = surface["use"](addon_metadata())
    with pytest.raises(HTTPException) as refused:
        await delete(ADDON, installations(("oak_other", None), claim))
    assert refused.value.status_code == 409 and refused.value.detail == DELETE_DETAIL
    assert "Revoke" in DELETE_DETAIL and "uninstall" in DELETE_DETAIL
    assert vault.deletes == [] and surface["log"].calls == [] and surface["evicted"] == []


async def test_a_superseded_add_on_credential_is_deletable(
    surface: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rotation without grace stores no previous key, so uninstall never removes the old one."""
    monkeypatch.setenv("SECRET_KEY", "a-test-secret-key-for-hmac")
    vault = surface["use"](addon_metadata())
    new_key, _secret, _meta = rotate_install_credential(
        vault, old_key_id=ADDON, extension_slug="backup-bot", extension_name="Backup Bot",
        install_id=7, organization_id=1, organization_slug="acme", capabilities=["inventory.read"],
        cluster_permissions=[{"cluster_id": "nuc1", "permissions": ["read"]}], created_by=PARTNER.email,
        expire_days=30, credential_generation=1, grace_seconds=0,
    )
    db = installations((new_key, None))
    assert await delete(ADDON, db) == {"success": True, "message": f"API key {ADDON} deleted"}
    assert not any(ADDON in path for path in vault.store)
    assert vault.deletes == [_FakeVault._path(ADDON, "metadata"), _FakeVault._path(ADDON, "secret")]
    assert surface["evicted"] == [ADDON]
    assert [c["action"] for c in surface["log"].calls] == ["integration.api_key.delete"]

    with pytest.raises(HTTPException) as refused:
        await delete(new_key, db)
    assert refused.value.status_code == 409


async def test_a_key_rotated_out_twice_is_deletable(surface: Dict[str, Any]) -> None:
    """Generation 3 is live and 2 is in its grace window, so generation 1 is claimed by nothing."""
    vault = surface["use"](addon_metadata())
    db = installations(("oak_gen3", "oak_gen2"))
    for claimed in ("oak_gen3", "oak_gen2"):
        vault.store[_FakeVault._path(claimed, "metadata")] = {**addon_metadata(), "id": claimed}
        vault.store[_FakeVault._path(claimed, "secret")] = {"hash": SECRETS[1], "raw": SECRETS[0]}
        with pytest.raises(HTTPException) as refused:
            await delete(claimed, db)
        assert refused.value.status_code == 409
    assert (await delete(ADDON, db))["success"] is True
    assert not any(ADDON in path for path in vault.store)
    assert [c["action"] for c in surface["log"].calls] == ["integration.api_key.delete"]


async def test_a_credential_the_reconcile_sweep_reclaimed_is_deletable(surface: Dict[str, Any]) -> None:
    """The sweep revokes an add-on key no installation claims. Nothing claims it now either."""
    vault = surface["use"](addon_metadata(is_revoked=True, is_active=False))
    assert (await delete(ADDON, installations(("oak_other", "oak_older"))))["success"] is True
    assert not any(ADDON in path for path in vault.store)


class _ClaimDown:
    """Answers the ownership read from real (empty) organization tables, then fails the claim read."""

    def __init__(self) -> None:
        self.db = sqlite_session(*org_tables())

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        if "extension_installations" in str(statement):
            raise ConnectionError("database is down: postgresql://user:pw@db/app")
        return await self.db.execute(statement, *args, **kwargs)


class _Down:
    async def execute(self, *_a: Any, **_k: Any) -> Any:
        raise ConnectionError("database is down: postgresql://user:pw@db/app")


async def test_a_claim_that_cannot_be_read_deletes_nothing(surface: Dict[str, Any]) -> None:
    vault = surface["use"](addon_metadata())
    with pytest.raises(HTTPException) as refused:
        await delete(ADDON, _ClaimDown())
    assert refused.value.status_code == 503 and refused.value.detail == CLAIM_UNKNOWN_DETAIL
    assert "postgresql" not in refused.value.detail
    assert vault.deletes == [] and surface["log"].calls == [] and surface["evicted"] == []


async def test_ownership_that_cannot_be_read_deletes_nothing(surface: Dict[str, Any]) -> None:
    """The ownership read comes first, so a database that answers nothing stops the delete there."""
    from app.services.integration_key_ownership import OWNERSHIP_UNKNOWN_DETAIL

    vault = surface["use"](addon_metadata())
    with pytest.raises(HTTPException) as refused:
        await delete(ADDON, _Down())
    assert refused.value.status_code == 503 and refused.value.detail == OWNERSHIP_UNKNOWN_DETAIL
    assert "postgresql" not in refused.value.detail
    assert vault.deletes == [] and surface["log"].calls == [] and surface["evicted"] == []


async def test_the_claim_reads_both_columns_for_this_key_only() -> None:
    db = installations(("oak_a", "oak_b"), ("oak_c", None))
    assert [await add_on_credential_edits.installation_claims(db, k) for k in ("oak_a", "oak_b", "oak_c")] == [
        True, True, True,
    ]
    assert await add_on_credential_edits.installation_claims(db, "oak_d") is False
    assert await add_on_credential_edits.installation_claims(installations(), "oak_a") is False


@pytest.mark.parametrize("route", ["capabilities", "permissions", "delete"])
async def test_a_key_outside_the_callers_clusters_is_still_404(
    surface: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """The 409 comes after the overlap check, so it never confirms an id the caller cannot see."""
    surface["use"](addon_metadata())
    monkeypatch.setattr(integrations, "get_current_allowed_clusters", lambda: {"nuc2"})
    call = {
        "capabilities": lambda: edit_capabilities(ADDON, ["inventory.read"]),
        "permissions": lambda: edit_permissions(ADDON, []),
        "delete": lambda: delete(ADDON, installations((ADDON, None))),
    }[route]
    with pytest.raises(HTTPException) as refused:
        await call()
    assert refused.value.status_code == 404


# ── allowed ────────────────────────────────────────────────────────────────────────────────────

async def test_an_ordinary_key_still_takes_all_three(surface: Dict[str, Any]) -> None:
    vault = surface["use"](plain_metadata())
    await edit_capabilities(PLAIN, ["inventory.read", "org.sync"])
    await edit_permissions(PLAIN, ["nuc1"])
    assert metadata_of(vault, PLAIN)["capabilities"] == ["inventory.read", "org.sync"]
    assert surface["reached"] and len(vault.writes) == 2
    db = installations((PLAIN, None))
    assert await delete(PLAIN, db) == {"success": True, "message": f"API key {PLAIN} deleted"}
    assert not any("extension_installations" in str(statement) for statement in db.statements), (
        "an ordinary key never reads the installations"
    )
    assert vault.deletes == [_FakeVault._path(PLAIN, "metadata"), _FakeVault._path(PLAIN, "secret")]


async def test_revoke_stays_available_as_the_kill_switch(surface: Dict[str, Any]) -> None:
    vault = surface["use"](addon_metadata())
    info = await integrations.revoke_api_key(ADDON, None, PARTNER, installations())
    assert info.is_revoked is True
    assert metadata_of(vault, ADDON)["is_revoked"] is True
    assert metadata_of(vault, ADDON)["extension_install_id"] == 7
    assert surface["evicted"] == [ADDON]
    assert [c["action"] for c in surface["log"].calls] == ["integration.api_key.revoke"]


async def test_the_installation_still_rescopes_and_rotates_after_a_revoke(
    surface: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rescope keeps the revocation, and rotation issues a working key again."""
    monkeypatch.setenv("SECRET_KEY", "a-test-secret-key-for-hmac")
    vault = surface["use"](addon_metadata())
    await integrations.revoke_api_key(ADDON, None, PARTNER, installations())

    class _Install:
        id, api_key_id = 7, ADDON
        granted_capabilities = ["inventory.read"]
        granted_scope = [{"cluster_id": "nuc1", "permissions": ["read"]}]

    extension_install._rewrite_key_grant(vault, _Install())
    rescoped = metadata_of(vault, ADDON)
    assert rescoped["capabilities"] == ["inventory.read"]
    assert rescoped["is_revoked"] is True

    new_key, _secret, _metadata = rotate_install_credential(
        vault, old_key_id=ADDON, extension_slug="backup-bot", extension_name="Backup Bot",
        install_id=7, organization_id=1, organization_slug="acme",
        capabilities=["inventory.read"], cluster_permissions=[], created_by=PARTNER.email,
        expire_days=30, credential_generation=1, grace_seconds=0,
    )
    assert metadata_of(vault, new_key)["is_revoked"] is False
    assert metadata_of(vault, new_key)["extension_install_id"] == 7
    assert metadata_of(vault, ADDON)["is_revoked"] is True


async def test_a_user_mapping_on_an_add_on_credential_is_allowed(
    surface: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mapping grants nothing: only the list count and the mapping list read it."""
    surface["use"](addon_metadata())
    saved: List[Tuple[str, Any]] = []
    monkeypatch.setattr(integrations, "save_user_mapping_to_vault", lambda _v, key, m: saved.append((key, m)))
    mapping = await integrations.add_user_mapping(
        ADDON,
        integrations.UserMappingRequest(external_user_id="u1", external_email="u1@example.test",
                                        cluster_permissions=[]),
        None, PARTNER,
    )
    assert saved == [(ADDON, mapping)]


# ── the table ──────────────────────────────────────────────────────────────────────────────────

DECIDED: Dict[Tuple[str, str], str] = {
    ("PUT", "/api-keys/{key_id}/capabilities"): "refused",
    ("PUT", "/api-keys/{key_id}/permissions"): "refused",
    ("DELETE", "/api-keys/{key_id}"): "refused",
    ("POST", "/api-keys/{key_id}/rotate-secret"): "refused",
    ("POST", "/api-keys/{key_id}/revoke"): "allowed",
    ("GET", "/api-keys/{key_id}"): "allowed",
    ("POST", "/api-keys/{key_id}/users"): "allowed",
    ("GET", "/api-keys/{key_id}/users"): "allowed",
    ("DELETE", "/api-keys/{key_id}/users/{user_id}"): "allowed",
}


def _key_routes() -> Set[Tuple[str, str]]:
    found: Set[Tuple[str, str]] = set()
    for route in integrations.router.routes:
        path: Optional[str] = getattr(route, "path", None)
        if path and "/api-keys/{key_id}" in path:
            for method in getattr(route, "methods", set()) or set():
                found.add((method, path.split("/integrations", 1)[-1]))
    return found


def test_every_keyed_route_has_a_decision_for_add_on_credentials() -> None:
    """A new /api-keys/{key_id} route fails here until someone decides what it does to an add-on key."""
    assert _key_routes() == set(DECIDED)
    assert "PUT    /api-keys/{id}/capabilities    refused" in add_on_credential_edits.__doc__


def test_the_refusals_name_no_secret() -> None:
    for detail in (GRANT_EDIT_DETAIL, DELETE_DETAIL):
        assert not any(secret in detail for secret in SECRETS)
        assert "7" not in detail, "the installation id stays out of the answer"
