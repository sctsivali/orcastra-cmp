"""An integration key granting a cluster another party holds is managed only by its own side.

Every single-key handler and the key list authorised by cluster overlap alone. On a cluster
bound to two partner organizations, each partner overlapped the other's keys, so one partner
could read another's grants and mapped users, and revoke or delete the key that runs the other
partner's OrcaHub sync and provisioning.
"""

from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

from app.core import org_scope
from app.core.auth import AuthenticatedUser
from app.models.db_models import Organization, OrganizationMember
from app.routers import integrations
from tests._sqlite_session import sqlite_session

SHARED = "cluster-shared"
OWN = "cluster-own"
ME = "partner-a@example.test"
TEAMMATE = "partner-a2@example.test"
OTHER = "partner-b@example.test"


def _user(email: str, groups: List[str] = ["role_partner"]) -> AuthenticatedUser:
    # note: resolve_roles caches by user_id, so each role gets its own id
    return AuthenticatedUser(user_id=f"{groups[0]}:{email}", email=email, groups=list(groups))


def _key(key_id: str, creator: str, cluster: str) -> Dict[str, Any]:
    return {
        "id": key_id,
        "name": key_id,
        "app_name": "qa-app",
        "created_at": "2026-09-01T00:00:00",
        "created_by": creator,
        "cluster_permissions": [{"cluster_id": cluster, "permissions": ["read", "write"], "projects": ["*"]}],
    }


@pytest.fixture
def world(monkeypatch):
    """SHARED is held by another party for ME, OWN is not. TEAMMATE shares ME's organization."""
    state = {"held_calls": 0}

    async def held(_db: Any, email: str, clusters: set) -> set:
        state["held_calls"] += 1
        return {c for c in clusters if c == SHARED}

    async def mates(_db: Any, email: str) -> set:
        return {ME, TEAMMATE} if email in (ME, TEAMMATE) else {OTHER}

    monkeypatch.setattr(integrations, "_clusters_held_by_another_party", held)
    monkeypatch.setattr(integrations, "_org_mate_emails", mates)
    return state


@pytest.mark.parametrize(
    "caller,key,kept",
    [
        (ME, _key("oak_mine", ME, SHARED), True),
        (ME, _key("oak_team", TEAMMATE, SHARED), True),
        (ME, _key("oak_theirs", OTHER, SHARED), False),
        (ME, _key("oak_own_cluster", OTHER, OWN), True),
        (ME, _key("oak_case", ME.upper(), SHARED), True),
        (ME, _key("oak_nobody", "", SHARED), False),
    ],
    ids=["creator", "same-organization", "other-party", "own-cluster", "case", "no-creator"],
)
async def test_who_manages_a_key(world, caller: str, key: Dict[str, Any], kept: bool) -> None:
    got = await integrations._keys_the_caller_manages(None, _user(caller), [key])
    assert (got == [key]) is kept


async def test_an_admin_manages_every_key(world) -> None:
    keys = [_key("oak_theirs", OTHER, SHARED)]
    assert await integrations._keys_the_caller_manages(None, _user(ME, ["role_admin"]), keys) == keys
    assert world["held_calls"] == 0


async def test_own_keys_skip_the_database(world) -> None:
    keys = [_key("oak_mine", ME, SHARED)]
    assert await integrations._keys_the_caller_manages(None, _user(ME), keys) == keys
    assert world["held_calls"] == 0


# -- every single-key handler -------------------------------------------------------------------

@pytest.fixture
def stored(monkeypatch, world):
    """The Vault read answers the other party's key, and the caller's scope overlaps it."""
    theirs = _key("oak_theirs", OTHER, SHARED)
    monkeypatch.setattr(integrations, "_read_api_key_metadata_blocking", lambda _k: dict(theirs))
    token = org_scope._allowed_clusters_var.set({SHARED, OWN})
    touched: List[str] = []
    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: touched.append("vault") or object())
    yield touched
    org_scope._allowed_clusters_var.reset(token)


def _handlers() -> List[Any]:
    user = _user(ME)
    return [
        lambda: integrations.get_api_key("oak_theirs", user=user, db=None),
        lambda: integrations.update_api_key_permissions(
            "oak_theirs", [integrations.ClusterPermission(cluster_id=SHARED)], None, user, None
        ),
        lambda: integrations.revoke_api_key("oak_theirs", None, user, None),
        lambda: integrations.delete_api_key("oak_theirs", None, user, None),
        lambda: integrations.add_user_mapping(
            "oak_theirs",
            integrations.UserMappingRequest(external_user_id="u1", external_email="u1@example.test"),
            None, user, None,
        ),
        lambda: integrations.list_user_mappings("oak_theirs", user=user, db=None),
    ]


@pytest.mark.parametrize("index", range(6), ids=["get", "permissions", "revoke", "delete", "add-mapping", "mappings"])
async def test_the_other_partys_key_reads_as_missing(stored, index: int) -> None:
    with pytest.raises(HTTPException) as caught:
        await _handlers()[index]()
    assert caught.value.status_code == 404
    assert stored == [], "the handler went on to Vault for a key the caller does not manage"


async def test_the_list_leaves_out_the_other_partys_key(monkeypatch, world) -> None:
    keys = [_key("oak_mine", ME, SHARED), _key("oak_theirs", OTHER, SHARED), _key("oak_old", OTHER, OWN)]

    class _Cache:
        def get(self, *_a: Any) -> None:
            return None

        def set(self, *_a: Any, **_k: Any) -> None:
            return None

    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: object())
    monkeypatch.setattr(integrations, "list_api_keys_from_vault", lambda _v: [dict(k) for k in keys])
    monkeypatch.setattr(integrations, "get_user_mappings_from_vault", lambda _v, _k: [])
    monkeypatch.setattr(integrations, "get_cache_manager", lambda: _Cache())
    token = org_scope._allowed_clusters_var.set({SHARED, OWN})
    try:
        answer = await integrations.list_api_keys(fresh=True, user=_user(ME), db=None)
    finally:
        org_scope._allowed_clusters_var.reset(token)
    assert sorted(k.id for k in answer.keys) == ["oak_mine", "oak_old"]


async def test_refresh_refuses_the_other_partys_key(monkeypatch, world) -> None:
    theirs = _key("oak_theirs", OTHER, SHARED)
    monkeypatch.setattr(integrations, "get_api_key_from_vault", lambda _v, _k: (dict(theirs), "hash"))
    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: object())
    called: List[str] = []

    async def no_call(*_a: Any, **_k: Any) -> None:
        called.append("callback")

    monkeypatch.setattr(integrations, "guarded_request", no_call)
    token = org_scope._allowed_clusters_var.set({SHARED})
    try:
        with pytest.raises(HTTPException) as caught:
            await integrations.refresh_organizations(api_key_id="oak_theirs", db=None, user=_user(ME))
    finally:
        org_scope._allowed_clusters_var.reset(token)
    assert caught.value.status_code == 404
    assert called == []


# -- who counts as the caller's organization ----------------------------------------------------

async def test_org_mates_are_members_and_creators_of_the_callers_organizations(monkeypatch) -> None:
    db = sqlite_session(Organization, OrganizationMember)
    db.add_all([
        Organization(id=1, name="A", slug="a", created_by="Founder-A@example.test", is_active=True),
        Organization(id=2, name="B", slug="b", created_by="founder-b@example.test", is_active=True),
        OrganizationMember(organization_id=1, user_email=ME, role="partner"),
        OrganizationMember(organization_id=1, user_email=TEAMMATE, role="partner"),
        OrganizationMember(organization_id=2, user_email=OTHER, role="partner"),
    ])
    await db.commit()
    mates = await integrations._org_mate_emails(db, ME)
    assert mates == {ME, TEAMMATE, "founder-a@example.test"}
    assert await integrations._org_mate_emails(db, "nobody@example.test") == {"nobody@example.test"}


# -- the management reads run off the loop -----------------------------------------------------

def test_no_async_handler_reads_a_key_from_vault_on_the_loop() -> None:
    """Only /metadata is cached now, so a key's /secret and mapping reads are real Vault round trips.

    They must run through asyncio.to_thread or run_blocking, never inline in an async body.
    """
    import ast
    from pathlib import Path

    source = (Path(integrations.__file__)).read_text()
    blocking = {"get_api_key_from_vault", "get_user_mappings_from_vault", "list_api_keys_from_vault",
                "_read_api_key_metadata_blocking"}
    offenders = []
    for fn in ast.walk(ast.parse(source)):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        stack = list(fn.body)
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in blocking:
                offenders.append(f"{fn.name}:{node.lineno}")
            stack.extend(ast.iter_child_nodes(node))
    assert offenders == []
