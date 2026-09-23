"""A Partner may not give an API key more of a shared cluster than they can see there.

Creation and edit checked cluster ids only. On a cluster another organization also holds, a
Partner could name that organization's project, or '*', and the key then acted on it through the
proxies and provisioning. The rule refuses only where another party holds the cluster: a buyer
org synced or provisioned through the Partner's own key is theirs, so the '*' an OrcaHub
connection carries still mints on a cluster whose only other holders are those buyers.
"""

from typing import Any, Dict, List, Optional

import pytest
from fastapi import HTTPException

from app.core import org_scope
from app.core.auth import AuthenticatedUser
from app.routers import integrations
from app.routers.integrations import APIKeyCreateRequest, ClusterPermission

PARTNER = "partner-a@example.test"

# (cluster_id, organization_id, registered_by, organization.api_key_id)
HOLDERS = [
    ("cluster-a", 1, None, None),
    ("cluster-a", 2, None, None),
    ("cluster-c", 1, None, None),
    ("cluster-c", 3, None, "oak_test"),
]


class _Result:
    def __init__(self, rows: List[Any]) -> None:
        self._rows = rows

    def all(self) -> List[Any]:
        return list(self._rows)

    def scalars(self) -> "_Result":
        return self


class FakeSession:
    def __init__(self, holders: List[tuple], own_ids: set) -> None:
        self.holders = holders
        self.own_ids = own_ids
        self.queries: List[str] = []

    async def execute(self, stmt: Any, *_a: Any, **_k: Any) -> _Result:
        text = str(stmt)
        self.queries.append(text)
        if "organization_clusters" in text:
            return _Result(self.holders)
        if "organization_members" in text:
            return _Result(sorted(self.own_ids))
        raise AssertionError(f"unexpected query: {text}")


class FakeVault:
    def __init__(self) -> None:
        self.writes: List[tuple] = []

    def vault_kv_put(self, path: str, data: Dict[str, Any]) -> None:
        self.writes.append((path, dict(data)))


class _Cache:
    def delete_pattern(self, *_a: Any, **_k: Any) -> None:
        return None

    def delete(self, *_a: Any, **_k: Any) -> None:
        return None

    def set(self, *_a: Any, **_k: Any) -> None:
        return None


@pytest.fixture
def env(monkeypatch):
    vault = FakeVault()
    state: Dict[str, Any] = {"creators": {"oak_test": PARTNER}}
    monkeypatch.setenv("SECRET_KEY", "unit-test-secret")
    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: vault)
    monkeypatch.setattr(integrations, "get_cache_manager", lambda: _Cache())

    async def _no_reconcile(*_a: Any, **_k: Any) -> int:
        return 0

    monkeypatch.setattr(integrations, "reconcile_personal_clusters", _no_reconcile)
    monkeypatch.setattr(
        integrations,
        "_read_key_creators_blocking",
        lambda key_ids: {k: v for k, v in state["creators"].items() if k in key_ids},
    )
    state["vault"] = vault
    return state


def _scope(allowed: Optional[set], visible: Optional[dict]) -> tuple:
    return (
        org_scope._allowed_clusters_var.set(allowed),
        org_scope._visible_projects_var.set(visible),
    )


def _reset(tokens: tuple) -> None:
    org_scope._allowed_clusters_var.reset(tokens[0])
    org_scope._visible_projects_var.reset(tokens[1])


PARTNER_SCOPE = (
    {"cluster-a", "cluster-c"},
    {"cluster-a": {"default", "proj-a"}, "cluster-c": {"default"}},
)


def _user() -> AuthenticatedUser:
    return AuthenticatedUser(user_id="partner-a", email=PARTNER, groups=["role_partner"])


def _create(cluster_id: str, projects: List[str]) -> APIKeyCreateRequest:
    return APIKeyCreateRequest(
        name="k",
        app_name="app",
        cluster_permissions=[
            ClusterPermission(cluster_id=cluster_id, permissions=["write"], projects=projects)
        ],
    )


async def _mint(cluster_id: str, projects: List[str], db: FakeSession, scope=PARTNER_SCOPE):
    tokens = _scope(*scope)
    try:
        return await integrations.create_api_key(
            request=_create(cluster_id, projects), _=None, _rbac=_user(), db=db
        )
    finally:
        _reset(tokens)


async def test_a_wildcard_on_a_cluster_another_org_holds_is_refused(env):
    with pytest.raises(HTTPException) as caught:
        await _mint("cluster-a", ["*"], FakeSession(HOLDERS, {1}))
    assert caught.value.status_code == 403
    assert "cluster-a/*" in str(caught.value.detail)
    assert env["vault"].writes == [], "a refused grant must not reach Vault"


async def test_a_project_outside_the_bound_is_refused(env):
    with pytest.raises(HTTPException) as caught:
        await _mint("cluster-a", ["proj-b"], FakeSession(HOLDERS, {1}))
    assert caught.value.status_code == 403
    assert "cluster-a/proj-b" in str(caught.value.detail)
    assert env["vault"].writes == []


async def test_projects_inside_the_bound_mint(env):
    response = await _mint("cluster-a", ["default", "proj-a"], FakeSession(HOLDERS, {1}))
    assert response.api_key.cluster_permissions[0].projects == ["default", "proj-a"]
    metadata = env["vault"].writes[0][1]
    assert metadata["cluster_permissions"][0]["projects"] == ["default", "proj-a"]


async def test_a_wildcard_where_only_the_partner_and_their_buyers_hold_mints(env):
    """OrcaHub compatibility: the connection key carries '*' on the partner's own cluster."""
    response = await _mint("cluster-c", ["*"], FakeSession(HOLDERS, {1}))
    assert response.api_key.cluster_permissions[0].projects == ["*"]
    assert env["vault"].writes, "the key must be written"


async def test_a_buyer_org_owned_by_someone_elses_key_is_another_holder(env):
    env["creators"] = {"oak_test": "partner-b@example.test"}
    with pytest.raises(HTTPException) as caught:
        await _mint("cluster-c", ["*"], FakeSession(HOLDERS, {1}))
    assert caught.value.status_code == 403
    assert "cluster-c/*" in str(caught.value.detail)


async def test_an_unreadable_key_creator_counts_as_another_holder(env):
    env["creators"] = {}
    with pytest.raises(HTTPException) as caught:
        await _mint("cluster-c", ["*"], FakeSession(HOLDERS, {1}))
    assert caught.value.status_code == 403


async def test_an_unbound_row_registered_by_someone_else_is_another_holder(env):
    holders = [("cluster-c", 1, None, None), ("cluster-c", None, "admin@example.test", None)]
    with pytest.raises(HTTPException) as caught:
        await _mint("cluster-c", ["*"], FakeSession(holders, {1}))
    assert caught.value.status_code == 403


async def test_an_unbound_row_the_caller_registered_is_theirs(env):
    holders = [("cluster-c", 1, None, None), ("cluster-c", None, PARTNER, None)]
    response = await _mint("cluster-c", ["*"], FakeSession(holders, {1}))
    assert response.api_key.cluster_permissions[0].projects == ["*"]


async def test_an_admin_is_not_bound(env):
    db = FakeSession(HOLDERS, {1})
    response = await _mint("cluster-a", ["*"], db, scope=(None, None))
    assert response.api_key.cluster_permissions[0].projects == ["*"]
    assert db.queries == [], "the admin path must not query holders at all"


async def test_a_vault_stall_resolving_creators_answers_503(env, monkeypatch):
    from app.core.blocking import BlockingTimeout

    async def _stall(*_a: Any, **_k: Any) -> None:
        raise BlockingTimeout("api_key_creators", 1.0)

    monkeypatch.setattr(integrations, "run_blocking", _stall)
    with pytest.raises(HTTPException) as caught:
        await _mint("cluster-c", ["*"], FakeSession(HOLDERS, {1}))
    assert caught.value.status_code == 503
    assert env["vault"].writes == []


def _stored(cluster_id: str, projects: List[str]) -> Dict[str, Any]:
    return {
        "id": "oak_legacy",
        "name": "legacy",
        "app_name": "app",
        "created_at": "2026-01-01T00:00:00",
        "cluster_permissions": [
            {"cluster_id": cluster_id, "projects": projects, "permissions": ["write"]}
        ],
    }


async def _update(env, monkeypatch, stored, permissions, db):
    monkeypatch.setattr(integrations, "_authorize_api_key", lambda _v, _k: stored)
    monkeypatch.setattr(
        integrations, "get_api_key_from_vault", lambda _v, _k: (dict(stored), "hash")
    )
    monkeypatch.setattr(integrations, "get_user_mappings_from_vault", lambda _v, _k: [])
    monkeypatch.setattr(integrations, "evict_api_key_cache", lambda _k: None)
    tokens = _scope(*PARTNER_SCOPE)
    try:
        return await integrations.update_api_key_permissions(
            key_id="oak_legacy", permissions=permissions, _=None, _rbac=_user(), db=db
        )
    finally:
        _reset(tokens)


async def test_an_update_keeps_what_the_key_already_holds(env, monkeypatch):
    stored = _stored("cluster-a", ["*"])
    permissions = [ClusterPermission(cluster_id="cluster-a", permissions=["write"], projects=["*"])]
    result = await _update(env, monkeypatch, stored, permissions, FakeSession(HOLDERS, {1}))
    assert result.cluster_permissions[0].projects == ["*"]
    assert env["vault"].writes, "carrying over an existing grant must still save"


async def test_an_update_cannot_add_past_the_bound(env, monkeypatch):
    stored = _stored("cluster-a", ["*"])
    permissions = [
        ClusterPermission(cluster_id="cluster-a", permissions=["write"], projects=["*", "proj-b"])
    ]
    with pytest.raises(HTTPException) as caught:
        await _update(env, monkeypatch, stored, permissions, FakeSession(HOLDERS, {1}))
    assert caught.value.status_code == 403
    assert "cluster-a/proj-b" in str(caught.value.detail)
    assert "cluster-a/*" not in str(caught.value.detail)
    assert env["vault"].writes == []
