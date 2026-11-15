"""An edit of a key's cluster access leaves an audit record with the grant before and after.

Create, the capability edit, revoke, rotate and delete were recorded; Edit Access was not, so a
partner could widen a key from read to admin, or add clusters, and the activity trail showed
nothing. The record carries only cluster ids, levels and project names, never the secret.
"""

from contextlib import asynccontextmanager
from typing import Any, Dict, List

import pytest

from app.core.auth import AuthenticatedUser
from app.routers import integrations


@pytest.mark.asyncio
async def test_widening_a_keys_access_is_recorded_with_before_and_after(monkeypatch) -> None:
    stored = {
        "id": "oak_x", "name": "QA", "app_name": "QA", "created_at": "2026-10-24T00:00:00+00:00",
        "cluster_permissions": [{"cluster_id": "nuc2", "permissions": ["read"], "projects": ["*"]}],
    }
    audits: List[Dict[str, Any]] = []

    @asynccontextmanager
    async def lock(_db, key_id):
        yield

    async def authorize(*_a, **_k):
        return dict(stored)

    async def nothing(*_a, **_k):
        return None

    class _Vault:
        def vault_kv_put(self, path, data):
            assert path.endswith("/metadata"), "only the metadata document is written"

    class _Cache:
        def delete_pattern(self, *_a):
            return None

    async def no_usage(keys):
        return keys

    async def stamped(requested, held=(), rebind=()):
        return list(requested)

    monkeypatch.setattr(integrations, "serialize_key_ownership", lock)
    monkeypatch.setattr(integrations, "_authorize_api_key", authorize)
    monkeypatch.setattr(integrations, "refuse_grant_edit", lambda metadata: None)
    monkeypatch.setattr(integrations, "get_current_allowed_clusters", lambda: None)
    monkeypatch.setattr(integrations, "_refuse_new_grants_to_unregistered_clusters", nothing)
    monkeypatch.setattr(integrations, "_refuse_a_grant_past_the_project_bound", nothing)
    monkeypatch.setattr(integrations, "release_key_locks", nothing)
    monkeypatch.setattr(integrations, "_assert_granted_projects_exist", nothing)
    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: _Vault())
    monkeypatch.setattr(integrations, "get_api_key_from_vault", lambda _v, _k, use_cache=True: (dict(stored), "hash"))
    monkeypatch.setattr(integrations, "get_user_mappings_from_vault", lambda *_a: [])
    monkeypatch.setattr(integrations, "get_cache_manager", lambda: _Cache())
    monkeypatch.setattr(integrations, "evict_api_key_cache", lambda key_id: None)
    monkeypatch.setattr(integrations, "_with_usage", no_usage)
    monkeypatch.setattr(integrations.cluster_generations, "stamped", stamped)
    monkeypatch.setattr(integrations, "_audit_credential_lifecycle", lambda **kw: audits.append(kw))

    request = [
        integrations.ClusterPermissionRequest(cluster_id="nuc2", permissions=["read", "write", "admin"], projects=["*"]),
        integrations.ClusterPermissionRequest(cluster_id="blitz", permissions=["read"], projects=["*"]),
    ]
    await integrations.update_api_key_permissions(
        "oak_x", request, _=None,
        _rbac=AuthenticatedUser(user_id="role_partner:p@example.test", email="p@example.test", groups=["role_partner"]),
        lxd_service=object(), db=object(),
    )

    assert len(audits) == 1
    record = audits[0]
    assert record["action"] == "integration.api_key.permissions.update" and record["key_id"] == "oak_x"
    extra = record["extra_details"]
    assert extra["cluster_permissions_before"] == [{"cluster_id": "nuc2", "permissions": ["read"], "projects": ["*"]}]
    assert {g["cluster_id"] for g in extra["cluster_permissions_after"]} == {"nuc2", "blitz"}
    assert "hash" not in repr(record) and "api_key_secret" not in repr(record)
