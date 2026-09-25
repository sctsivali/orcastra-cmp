"""The key routes refuse to change an add-on credential's grant, and keep its kill switch.

An add-on's credential is an integration key, so PUT /api-keys/{id}/capabilities and
/permissions reached it for any partner whose clusters overlapped its grant. That changed what a
third party holds outside the operator's install consent. The installation owns the grant now:
those edits answer 409 and name rescope. Revoke stays, and the installation's own flows (rescope,
rotation, the reconcile sweep) keep working after one.
"""

from typing import Any, Dict, List, Optional, Set, Tuple

import pytest
from fastapi import HTTPException

from app.routers import integrations
from app.services import add_on_credential_edits, extension_install
from app.services.add_on_credential_edits import DELETE_DETAIL, GRANT_EDIT_DETAIL
from app.services.extension_credentials import build_install_metadata, rotate_install_credential
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


async def test_deleting_an_add_on_credential_is_refused(surface: Dict[str, Any]) -> None:
    vault = surface["use"](addon_metadata())
    with pytest.raises(HTTPException) as refused:
        await integrations.delete_api_key(ADDON, None, PARTNER)
    assert refused.value.status_code == 409 and refused.value.detail == DELETE_DETAIL
    assert "Revoke" in DELETE_DETAIL and "uninstall" in DELETE_DETAIL
    assert vault.deletes == [] and surface["log"].calls == [] and surface["evicted"] == []


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
        "delete": lambda: integrations.delete_api_key(ADDON, None, PARTNER),
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
    assert await integrations.delete_api_key(PLAIN, None, PARTNER) == {
        "success": True, "message": f"API key {PLAIN} deleted",
    }
    assert vault.deletes == [_FakeVault._path(PLAIN, "metadata"), _FakeVault._path(PLAIN, "secret")]


async def test_revoke_stays_available_as_the_kill_switch(surface: Dict[str, Any]) -> None:
    vault = surface["use"](addon_metadata())
    info = await integrations.revoke_api_key(ADDON, None, PARTNER)
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
    await integrations.revoke_api_key(ADDON, None, PARTNER)

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
