"""State that belongs to a whole cluster is read or changed only where no other party holds it.

LXD auth groups and identities and storage pools belong to no project. A partner held to a project
bound on a cluster another party holds could still list another party's groups and identities,
create a group with any permission list, add an identity to one, delete either, and create,
reconfigure or delete a pool that party's projects use, because every route checked the cluster
only. The party is read from real rows on SQLite, and the LXD client and the configuration lock are
recorders, so a refusal is proven to land before either is reached.
"""

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import OperationalError

from app.core import org_scope
from app.core.auth import AuthenticatedUser
from app.core.whole_cluster_access import SHARED_CLUSTER_DETAIL
from app.models.db_models import Organization, OrganizationCluster, OrganizationMember, OrganizationOwnerKey
from app.routers import permissions, storage_pools
from app.services.lxd.fanout import NodeRead
from tests._sqlite_session import sqlite_session

SELLER = "seller@example.test"
OTHER = "other-partner@example.test"
ADMIN = "ops@example.test"
KEY = "oak_seller"
OWN, MIXED, SOLO = "own", "mixed", "solo"
REACHED = 418


def _user(email: str, group: str = "role_partner") -> AuthenticatedUser:
    return AuthenticatedUser(user_id=f"{group}:{email}", email=email, groups=[group])


async def _world():
    """A sale on the seller's own cluster, and another partner beside the seller on 'mixed'."""
    db = sqlite_session(Organization, OrganizationMember, OrganizationCluster, OrganizationOwnerKey)
    db.add_all([
        Organization(id=1, name="home", slug="home", created_by="system", is_active=True, source="orcahub", api_key_id=KEY),
        Organization(id=2, name="buyer", slug="buyer", created_by=SELLER, is_active=True, source="orcahub", api_key_id=KEY),
        Organization(id=3, name="other", slug="other", created_by="system", is_active=True, source="orcahub", api_key_id="oak_other"),
        Organization(id=4, name="solo", slug="solo", created_by=OTHER, is_active=True, source="orcahub", api_key_id="oak_other"),
    ])
    await db.commit()
    db.add_all([
        OrganizationMember(organization_id=1, user_email=SELLER, role="partner"),
        OrganizationMember(organization_id=3, user_email=OTHER, role="partner"),
        OrganizationCluster(organization_id=1, cluster_id=OWN, registered_by=SELLER),
        OrganizationCluster(organization_id=2, cluster_id=OWN),
        OrganizationCluster(organization_id=1, cluster_id=MIXED, registered_by=SELLER),
        OrganizationCluster(organization_id=3, cluster_id=MIXED),
        # The seller buys from the other partner: a member row reaches 'solo', which only the
        # other partner's organization holds, so no binding count ever bounds it.
        OrganizationMember(organization_id=4, user_email=SELLER, role="member"),
        OrganizationCluster(organization_id=4, cluster_id=SOLO, registered_by=OTHER),
    ])
    await db.commit()
    return db


class _Down:
    async def execute(self, *_a: Any, **_k: Any) -> Any:
        raise OperationalError("SELECT 1", {}, Exception("connection lost"))


@pytest.fixture
def scope():
    """The seller's request: both clusters reachable, the project bound engaged on 'mixed' only."""
    clusters = org_scope._allowed_clusters_var.set({OWN, MIXED, SOLO})
    bound = org_scope._visible_projects_var.set({MIXED: {"default"}})
    yield
    org_scope._visible_projects_var.reset(bound)
    org_scope._allowed_clusters_var.reset(clusters)


@pytest.fixture
def lxd(monkeypatch: pytest.MonkeyPatch) -> Dict[str, List[str]]:
    """The LXD client, the pool configuration lock and each fleet read, recorded, never reached for real."""
    reached: Dict[str, List[str]] = {"client": [], "lock": [], "identities": [], "groups": []}

    async def _client_for(_service: Any, host: str) -> Any:
        reached["client"].append(host)
        raise HTTPException(status_code=REACHED, detail="the LXD client was reached")

    def _lock(_db: Any, host: str) -> Any:
        reached["lock"].append(host)
        raise HTTPException(status_code=REACHED, detail="the configuration lock was reached")

    def _read(kind: str) -> Any:
        def read(_service: Any, node: str) -> NodeRead:
            reached[kind].append(node)
            return NodeRead(rows=[])
        return read

    monkeypatch.setattr(permissions, "client_for", _client_for)
    monkeypatch.setattr(storage_pools, "serialize_cluster_configuration", _lock)
    monkeypatch.setattr(permissions, "_fetch_identities_for_node", _read("identities"))
    monkeypatch.setattr(permissions, "_fetch_groups_for_node", _read("groups"))
    return reached


def _fleet() -> Any:
    names = lambda: [OWN, MIXED, SOLO]  # noqa: E731 - a stub method
    return SimpleNamespace(connection_manager=SimpleNamespace(get_node_names=names), get_node_names=names)


def _single_routes(host: str, user: AuthenticatedUser, db: Any) -> Dict[str, Any]:
    service = _fleet()
    return {
        "get-identity": lambda: permissions.get_identity("id1", host, service, db, user),
        "get-group": lambda: permissions.get_group("g1", host, service, db, user),
        "create-group": lambda: permissions.create_group("g1", "", [], host, service, None, db, user),
        "update-group": lambda: permissions.update_group(
            "g1", permissions.GroupUpdateRequest(permissions=[{"entity_type": "server", "entitlement": "admin"}]),
            host, service, None, db, user),
        "delete-group": lambda: permissions.delete_group("g1", host, service, None, db, user),
        "add-identity": lambda: permissions.add_identity_to_group(
            "g1", permissions.IdentityAddRequest(auth_type="tls", identity_id="id1"), host, service, None, db, user),
        "remove-identity": lambda: permissions.remove_identity_from_group("g1", "tls", "id1", host, service, None, db, user),
        "delete-identity": lambda: permissions.delete_identity("tls", "id1", host, service, None, db, user),
        "list-identities": lambda: permissions.list_identities(host, service, db, user),
        "list-groups": lambda: permissions.list_groups(host, service, db, user),
        "create-pool": lambda: storage_pools.create_storage_pool(
            storage_pools.StoragePoolCreateRequest(name="p", driver="dir"), host, service, None, user, db),
        "update-pool": lambda: storage_pools.update_storage_pool("p", host, {"size": "1GiB"}, None, service, None, user, db),
        "delete-pool": lambda: storage_pools.delete_storage_pool("p", host, service, None, user, db),
    }


ROUTES = list(_single_routes(OWN, _user(SELLER), None))
POOL_ROUTES = {"create-pool", "update-pool", "delete-pool"}
FLEET_READS = {"list-identities": "identities", "list-groups": "groups"}


def _reached(lxd: Dict[str, List[str]], route: str) -> List[str]:
    return lxd["lock"] if route in POOL_ROUTES else lxd[FLEET_READS[route]] if route in FLEET_READS else lxd["client"]


@pytest.mark.parametrize("route", ROUTES)
async def test_a_cluster_another_party_holds_is_refused_before_lxd_is_reached(scope, lxd, route: str) -> None:
    db = await _world()
    with pytest.raises(HTTPException) as caught:
        await _single_routes(MIXED, _user(SELLER), db)[route]()
    assert caught.value.status_code == 403 and caught.value.detail == SHARED_CLUSTER_DETAIL
    assert lxd == {"client": [], "lock": [], "identities": [], "groups": []}


@pytest.mark.parametrize("route", ROUTES)
async def test_a_cluster_another_party_holds_alone_is_refused_with_no_bound(scope, lxd, route: str) -> None:
    """The count-based bound never engages on 'solo' (one binding), so only the party read refuses it."""
    db = await _world()
    assert org_scope.visible_projects_for(SOLO) is None
    with pytest.raises(HTTPException) as caught:
        await _single_routes(SOLO, _user(SELLER), db)[route]()
    assert caught.value.status_code == 403 and caught.value.detail == SHARED_CLUSTER_DETAIL
    assert lxd == {"client": [], "lock": [], "identities": [], "groups": []}


@pytest.mark.parametrize("route", ROUTES)
async def test_the_partners_own_cluster_after_a_sale_is_the_partners(scope, lxd, route: str) -> None:
    db = await _world()
    try:
        await _single_routes(OWN, _user(SELLER), db)[route]()
    except HTTPException as exc:
        assert exc.status_code == REACHED
    assert _reached(lxd, route) == [OWN]


@pytest.mark.parametrize("route", ROUTES)
async def test_an_admin_is_not_held_and_the_database_is_not_asked(lxd, route: str) -> None:
    db = await _world()
    try:
        await _single_routes(MIXED, _user(ADMIN, "role_admin"), db)[route]()
    except HTTPException as exc:
        assert exc.status_code == REACHED
    assert _reached(lxd, route) == [MIXED] and db.statements == []


@pytest.mark.parametrize("route", [r for r in ROUTES if r not in FLEET_READS])
async def test_a_database_that_cannot_say_who_holds_it_changes_nothing(scope, lxd, route: str) -> None:
    with pytest.raises(HTTPException) as caught:
        await _single_routes(MIXED, _user(SELLER), _Down())[route]()
    assert caught.value.status_code == 503
    assert lxd == {"client": [], "lock": [], "identities": [], "groups": []}


# ── the fleet listings ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("route,kind", [(permissions.list_identities, "identities"), (permissions.list_groups, "groups")])
async def test_a_fleet_listing_leaves_out_another_partys_cluster_entirely(scope, lxd, route: Any, kind: str) -> None:
    db = await _world()
    answer = await route(None, _fleet(), db, _user(SELLER))
    assert lxd[kind] == [OWN]
    assert answer.nodes == [OWN] and answer.unreachable_nodes == []
    assert MIXED not in str(answer.model_dump()) and SOLO not in str(answer.model_dump())


@pytest.mark.parametrize("route,kind", [(permissions.list_identities, "identities"), (permissions.list_groups, "groups")])
async def test_a_fleet_listing_narrows_when_the_database_cannot_answer(scope, lxd, route: Any, kind: str) -> None:
    answer = await route(None, _fleet(), _Down(), _user(SELLER))
    # note: on this line every non-admin cluster is judged by party, so a database that cannot
    # answer leaves every one of them out, the seller's own included.
    assert lxd[kind] == [] and answer.nodes == [] and answer.unreachable_nodes == []


@pytest.mark.parametrize("route", [permissions.list_identities, permissions.list_groups])
async def test_a_fleet_listing_keeps_its_own_http_status(scope, lxd, route: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal raised while working out what to leave out is not turned into a 500 naming it."""
    async def _unavailable(*_a: Any, **_k: Any) -> set:
        raise HTTPException(status_code=503, detail="Could not confirm who holds these clusters")

    monkeypatch.setattr(permissions, "clusters_held_elsewhere", _unavailable)
    with pytest.raises(HTTPException) as caught:
        await route(None, _fleet(), None, _user(SELLER))
    assert caught.value.status_code == 503


@pytest.mark.parametrize("route", ROUTES)
async def test_the_party_decides_even_where_a_request_bound_engaged(lxd, route: str) -> None:
    """A bound alone never refuses: on the seller's own cluster the party read finds only its side."""
    db = await _world()
    clusters = org_scope._allowed_clusters_var.set({OWN, MIXED})
    bound = org_scope._visible_projects_var.set({OWN: {"default"}, MIXED: {"default"}})
    try:
        await _single_routes(OWN, _user(SELLER), db)[route]()
    except HTTPException as exc:
        assert exc.status_code == REACHED
    finally:
        org_scope._visible_projects_var.reset(bound)
        org_scope._allowed_clusters_var.reset(clusters)
    assert _reached(lxd, route) == [OWN]
