"""Only Partners and Admins read API keys and their user mappings.

The integrations router admits every tier, and the key list, the key detail and the mapping list
added nothing on top. A tenant whose clusters overlapped a key could read its cluster and project
grants and every mapping's external email, name and permissions. The sibling write routes were
already partner or admin only.
"""

from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import integrations

METADATA: Dict[str, Any] = {
    "id": "oak_test",
    "name": "k",
    "app_name": "app",
    "created_at": "2026-01-01T00:00:00",
    "cluster_permissions": [{"cluster_id": "cluster-a", "projects": ["*"], "permissions": ["read"]}],
}


class _Cache:
    def get(self, *_a: Any) -> None:
        return None

    def set(self, *_a: Any, **_k: Any) -> None:
        return None


async def _returns_metadata(_key_id: str, _user: Any, _db: Any) -> Dict[str, Any]:
    return dict(METADATA)


async def _keeps_every_key(_db: Any, _user: Any, keys: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return keys


@pytest.fixture(autouse=True)
def vault(monkeypatch):
    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: object())
    monkeypatch.setattr(integrations, "_authorize_api_key", _returns_metadata)
    monkeypatch.setattr(integrations, "get_api_key_from_vault", lambda _v, _k: (dict(METADATA), "h"))
    monkeypatch.setattr(integrations, "list_api_keys_from_vault", lambda _v: [dict(METADATA)])
    monkeypatch.setattr(integrations, "get_user_mappings_from_vault", lambda _v, _k: [])
    monkeypatch.setattr(integrations, "get_cache_manager", lambda: _Cache())
    monkeypatch.setattr(integrations, "_keys_the_caller_manages", _keeps_every_key)


def _client(groups: List[str]) -> TestClient:
    from app.core.auth import AuthenticatedUser
    from app.core.rbac import get_current_user

    app = FastAPI()
    app.include_router(integrations.router)
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        user_id=groups[0], email=f"{groups[0]}@example.test", groups=groups
    )
    app.dependency_overrides[integrations.org_scope_dependency] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


PATHS = ["/integrations/api-keys", "/integrations/api-keys/oak_test", "/integrations/api-keys/oak_test/users"]


@pytest.mark.parametrize("path", PATHS)
def test_a_tenant_is_refused(path):
    response = _client(["role_tenant"]).get(path)
    assert response.status_code == 403, response.text[:200]


@pytest.mark.parametrize("groups", [["role_partner"], ["role_admin"]])
@pytest.mark.parametrize("path", PATHS)
def test_a_partner_and_an_admin_are_served(groups, path):
    response = _client(groups).get(path)
    assert response.status_code == 200, response.text[:200]
