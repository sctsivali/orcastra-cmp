"""A key holding a grant on a cluster its creator reaches only by purchase can still be narrowed.

Such a key keeps its grant but gains no capability through it. The capability edit applied that
refusal to every edit, so removing instance.exec from the key answered 403. Only an edit that adds
a capability is refused now; a narrowing goes through.
"""

from contextlib import asynccontextmanager
from typing import Any, List

import pytest
from fastapi import HTTPException

from app.core.auth import AuthenticatedUser
from app.routers import integrations

PARTNER = AuthenticatedUser(user_id="role_partner:p@example.test", email="p@example.test", groups=["role_partner"])


@pytest.fixture
def key_on_a_purchase(monkeypatch) -> List[Any]:
    stored = {"id": "oak_x", "name": "QA", "app_name": "QA", "created_at": "2026-10-24T00:00:00+00:00",
              "capabilities": ["inventory.read", "instance.exec"],
              "cluster_permissions": [{"cluster_id": "bought", "permissions": ["read"], "projects": ["*"]}]}
    written: List[Any] = []

    @asynccontextmanager
    async def lock(*_a, **_k):
        yield

    async def nothing(*_a, **_k):
        return None

    async def owned_ids(*_a, **_k):
        return []

    async def write(_db, _key_id, _handed, do_write):
        do_write()

    def refuse_purchased(cluster_ids):
        if "bought" in set(cluster_ids):
            raise HTTPException(status_code=403, detail="purchased")

    class _Vault:
        def vault_kv_put(self, path, data):
            written.append(sorted(data["capabilities"]))

    class _Cache:
        def delete_pattern(self, *_a):
            return None

    async def no_usage(keys):
        return keys

    monkeypatch.setattr(integrations, "_authorize_api_key", nothing)
    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: _Vault())
    monkeypatch.setattr(integrations, "serialize_key_ownership", lock)
    monkeypatch.setattr(integrations, "serialize_org_owners", lock)
    monkeypatch.setattr(integrations, "owned_org_ids_to_lock", owned_ids)
    monkeypatch.setattr(integrations, "get_api_key_from_vault", lambda _v, _k, use_cache=True: (dict(stored), "h"))
    monkeypatch.setattr(integrations, "refuse_grant_edit", lambda metadata: None)
    monkeypatch.setattr(integrations, "refuse_grants_on_purchased_clusters", refuse_purchased)
    monkeypatch.setattr(integrations, "_write_key_then_commit", write)
    monkeypatch.setattr(integrations, "record_owner_handoffs", lambda *a, **k: None)
    monkeypatch.setattr(integrations, "evict_api_key_cache", lambda key_id: None)
    monkeypatch.setattr(integrations, "get_cache_manager", lambda: _Cache())
    monkeypatch.setattr(integrations, "_audit_credential_lifecycle", lambda **kw: None)
    monkeypatch.setattr(integrations, "get_user_mappings_from_vault", lambda *_a: [])
    monkeypatch.setattr(integrations, "_with_usage", no_usage)
    return written


async def test_removing_a_capability_goes_through(key_on_a_purchase) -> None:
    request = integrations.APIKeyCapabilitiesRequest(capabilities=["inventory.read"])
    await integrations.update_api_key_capabilities("oak_x", request, _=None, _rbac=PARTNER, db=object())
    assert key_on_a_purchase == [["inventory.read"]]


async def test_adding_a_capability_is_still_refused(key_on_a_purchase) -> None:
    request = integrations.APIKeyCapabilitiesRequest(capabilities=["inventory.read", "instance.exec", "instance.files"])
    with pytest.raises(HTTPException) as refused:
        await integrations.update_api_key_capabilities("oak_x", request, _=None, _rbac=PARTNER, db=object())
    assert refused.value.status_code == 403 and key_on_a_purchase == []


async def test_a_legacy_key_cannot_gain_guest_access_through_a_purchase(key_on_a_purchase, monkeypatch) -> None:
    """A legacy key holds every capability but guest access, so exec or desktop input is a gain."""
    legacy = {"id": "oak_x", "name": "QA", "app_name": "QA", "created_at": "2026-10-24T00:00:00+00:00",
              "cluster_permissions": [{"cluster_id": "bought", "permissions": ["read"], "projects": ["*"]}]}
    monkeypatch.setattr(integrations, "get_api_key_from_vault", lambda _v, _k, use_cache=True: (dict(legacy), "h"))
    request = integrations.APIKeyCapabilitiesRequest(capabilities=["inventory.read", "instance.exec"])
    with pytest.raises(HTTPException) as refused:
        await integrations.update_api_key_capabilities("oak_x", request, _=None, _rbac=PARTNER, db=object())
    assert refused.value.status_code == 403 and key_on_a_purchase == []


async def test_a_legacy_key_naming_a_narrower_set_goes_through(key_on_a_purchase, monkeypatch) -> None:
    # Withdrawing org.sync runs the owner check, which owns nothing here.
    async def owns_nothing(*_a, **_k):
        return []

    monkeypatch.setattr(integrations, "_refuse_ending_an_owner", owns_nothing)
    monkeypatch.setattr(integrations, "apply_owner_handoffs", lambda *_a, **_k: _empty())
    monkeypatch.setattr(integrations, "confirm_provisioning_withdrawal", lambda *_a, **_k: _empty())
    legacy = {"id": "oak_x", "name": "QA", "app_name": "QA", "created_at": "2026-10-24T00:00:00+00:00",
              "cluster_permissions": [{"cluster_id": "bought", "permissions": ["read"], "projects": ["*"]}]}
    monkeypatch.setattr(integrations, "get_api_key_from_vault", lambda _v, _k, use_cache=True: (dict(legacy), "h"))
    request = integrations.APIKeyCapabilitiesRequest(capabilities=["inventory.read"])
    await integrations.update_api_key_capabilities("oak_x", request, _=None, _rbac=PARTNER, db=object())
    assert key_on_a_purchase == [["inventory.read"]]


async def _empty():
    return []
