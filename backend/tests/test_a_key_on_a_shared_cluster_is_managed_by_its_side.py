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
from app.models.db_models import Organization, OrganizationCluster, OrganizationMember, OrganizationOwnerKey
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

    async def linked(_db: Any, email: str) -> set:
        """The integration keys carried by the orgs the caller runs: ME's team key is oak_team."""
        return {"oak_team"} if email in (ME, TEAMMATE) else set()

    monkeypatch.setattr(integrations, "_clusters_held_by_another_party", held)
    monkeypatch.setattr(integrations, "_run_org_keys", linked)
    return state


@pytest.mark.parametrize(
    "caller,key,kept",
    [
        (ME, _key("oak_mine", ME, SHARED), True),
        (ME, _key("oak_team", TEAMMATE, SHARED), True),
        (ME, _key("oak_admin_made", "ops@example.test", SHARED), False),
        (ME, _key("oak_theirs", OTHER, SHARED), False),
        (ME, _key("oak_own_cluster", OTHER, OWN), True),
        (ME, _key("oak_case", ME.upper(), SHARED), True),
        (ME, _key("oak_nobody", "", SHARED), False),
    ],
    ids=["creator", "key-syncs-my-org", "admin-minted-elsewhere", "other-party", "own-cluster", "case", "no-creator"],
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
        lambda: integrations.rotate_api_key_secret("oak_theirs", None, user, None),
        lambda: integrations.delete_api_key("oak_theirs", None, user, None),
        lambda: integrations.add_user_mapping(
            "oak_theirs",
            integrations.UserMappingRequest(external_user_id="u1", external_email="u1@example.test"),
            None, user, None,
        ),
        lambda: integrations.list_user_mappings("oak_theirs", user=user, db=None),
    ]


@pytest.mark.parametrize(
    "index", range(7), ids=["get", "permissions", "revoke", "rotate", "delete", "add-mapping", "mappings"]
)
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


# -- on real rows: a synced buyer founder is not on the seller's side ---------------------------
#
# OrcaHub makes every organization's founder a PARTNER, so the sync writes a partner row in each
# buyer organization, and every buyer carries the seller's key. "Manages a key that syncs an
# organization I run" would therefore hand a buyer's founder the seller's integration key. Only
# the key's home organization lends it (services/cluster_parties.side_keys).

SELLER, FOUNDER = "p@example.com", "founder@example.com"


async def _sale(**overrides: Any) -> Any:
    """The seller's home (synced, its partner registered the cluster) and two buyers its key provisioned."""
    db = sqlite_session(Organization, OrganizationMember, OrganizationCluster, OrganizationOwnerKey)
    orgs = overrides.get("orgs", [(1, "system"), (2, SELLER), (3, SELLER)])
    db.add_all([Organization(id=i, name=f"o{i}", slug=f"o{i}", created_by=by, is_active=True, api_key_id="oak_a",
                             source="orcahub") for i, by in orgs])
    await db.commit()
    db.add_all([OrganizationMember(organization_id=o, user_email=e, role=r)
                for o, e, r in overrides.get("members", [(1, SELLER, "partner"), (2, FOUNDER, "partner")])])
    db.add_all([OrganizationCluster(organization_id=o, cluster_id="own", registered_by=by)
                for o, by in overrides.get("bindings", [(1, SELLER), (2, None), (3, None)])])
    await db.commit()
    return db


SELLER_KEY = {"id": "oak_a", "created_by": "ops@example.com",
              "cluster_permissions": [{"cluster_id": "own", "permissions": ["read"], "projects": ["*"]}]}


async def test_a_buyer_founder_cannot_manage_the_sellers_key() -> None:
    db = await _sale()
    assert await integrations._keys_the_caller_manages(db, _user(FOUNDER), [SELLER_KEY]) == [], (
        "a buyer founder could revoke or delete the seller's integration key"
    )
    assert await integrations._keys_the_caller_manages(db, _user(SELLER), [SELLER_KEY]) == [SELLER_KEY]


async def test_a_teammate_of_the_seller_manages_the_key_its_home_syncs() -> None:
    db = await _sale(members=[(1, SELLER, "partner"), (1, "mate@example.com", "partner"), (2, FOUNDER, "partner")])
    assert await integrations._keys_the_caller_manages(db, _user("mate@example.com"), [SELLER_KEY]) == [SELLER_KEY]


async def test_a_home_synced_again_after_a_buyer_lends_the_key_to_nobody() -> None:
    """The seller's home was deleted and synced again, so a buyer is the oldest org on the key. It
    registered no cluster the key sold on, so no partner row lends the key: the founder stays out,
    and the seller is narrowed until an administrator fixes the home."""
    db = await _sale(orgs=[(2, "system"), (3, "ops@example.com"), (5, "system")],
                     members=[(2, FOUNDER, "partner"), (5, SELLER, "partner")],
                     bindings=[(2, None), (3, None), (5, SELLER)])
    for email in (FOUNDER, SELLER):
        assert await integrations._keys_the_caller_manages(db, _user(email), [SELLER_KEY]) == []


async def test_a_founder_binding_their_own_cluster_to_two_buyers_lends_no_key() -> None:
    """The oldest synced organization on the seller's key is a buyer (the seller's home was synced
    again later). Its founder registers a cluster of their own and binds it to that buyer and to a
    second buyer on the same key, which looks like a sale from a home. A sale is a binding
    provisioning made, never one the caller registered, so it lends nothing."""
    from app.services.cluster_parties import clusters_held_by_another_party, side_keys

    db = await _sale(
        orgs=[(2, "system"), (3, "system"), (5, "system")],
        members=[(2, FOUNDER, "partner"), (3, FOUNDER, "partner"), (5, SELLER, "partner")],
        bindings=[(5, SELLER), (2, None), (3, None)],
    )
    db.add_all([OrganizationCluster(organization_id=2, cluster_id="founders-own", registered_by=FOUNDER)])
    await db.commit()
    db.add_all([OrganizationCluster(organization_id=3, cluster_id="founders-own", registered_by=FOUNDER)])
    await db.commit()
    assert await side_keys(db, FOUNDER) == set()
    assert await integrations._keys_the_caller_manages(db, _user(FOUNDER), [SELLER_KEY]) == [], (
        "a buyer founder could manage, and then rotate, the seller's integration key"
    )
    # note: on this line the same side decides the partner project bound, so the seller's cluster
    # stays another party's to the founder there too.
    assert await clusters_held_by_another_party(db, FOUNDER, {"own"}) == {"own"}


async def test_a_sale_made_by_hand_lends_the_seller_nothing() -> None:
    """Fail-closed on purpose: a buyer bound to the seller's cluster from the dashboard, not by
    provisioning, is not a sale, so it puts the key on nobody's side through the home."""
    from app.services.cluster_parties import side_keys

    synced = [(1, "system"), (2, "system"), (3, "system")]
    by_hand = await _sale(orgs=synced, bindings=[(1, SELLER), (2, SELLER), (3, SELLER)])
    assert await side_keys(by_hand, SELLER) == set()
    provisioned = await _sale(orgs=synced)
    assert await side_keys(provisioned, SELLER) == {"oak_a"}
