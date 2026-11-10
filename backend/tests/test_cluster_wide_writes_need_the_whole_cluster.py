"""Writes that change a cluster for every holder need the whole cluster.

Deregistering a cluster ends it for every organization that holds it, a new certificate replaces the
owner's pending renewal, and the name and location are the cluster's own. A bridge network, an OVN
uplink and the ACLs they use live in the default project, which a project bound always leaves
visible, so writing one changes the cluster for every project on it. The cluster scope alone is not
enough for any of these. The party is read from real rows on SQLite; everything past the guard is a
recorder.
"""

import inspect
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

from app.core import org_scope
from app.core.auth import AuthenticatedUser
from app.core.whole_cluster_access import SHARED_CLUSTER_DETAIL, verify_default_project_write
from app.models.schemas import ClusterLocationUpdateRequest, NodeUpdateRequest
from app.routers import cluster_registration, network_acls, networks, nodes
from tests.test_whole_cluster_state_needs_the_whole_cluster import ADMIN, MIXED, OWN, SELLER, SOLO, _world

REACHED = 418
JAKARTA = {
    "region_id": "c-id", "region_name": "Indonesia", "country_code": "ID", "country_name": "Indonesia",
    "city": "Indonesia", "latitude": -2.5, "longitude": 118.0, "timezone": "Asia/Jakarta", "continent": "AS",
}


def _user(email: str, group: str = "role_partner") -> AuthenticatedUser:
    return AuthenticatedUser(user_id=f"{group}:{email}", email=email, groups=[group])


@pytest.fixture
def scope():
    clusters = org_scope._allowed_clusters_var.set({OWN, MIXED, SOLO})
    bound = org_scope._visible_projects_var.set({MIXED: {"default"}})
    yield
    org_scope._visible_projects_var.reset(bound)
    org_scope._allowed_clusters_var.reset(clusters)


@pytest.fixture
def reached(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """The first step past the guard on each route, recorded and stopped."""
    seen: List[str] = []

    async def _fence(cluster_id: str, **_k: Any) -> None:
        seen.append(cluster_id)
        raise HTTPException(status_code=REACHED, detail="reached")

    monkeypatch.setattr(cluster_registration, "assert_cluster_not_migration_fenced", _fence)
    return seen


class _Cert:
    def __init__(self, seen: List[str]) -> None:
        self.seen = seen

    def get_cluster_info(self, cluster_id: str) -> Dict[str, Any]:
        self.seen.append(cluster_id)
        raise HTTPException(status_code=REACHED, detail="reached")


def _routes(cluster: str, user: AuthenticatedUser, db: Any, seen: List[str]) -> Dict[str, Any]:
    cert = _Cert(seen)

    def _node_names() -> List[str]:
        seen.append(cluster)
        raise HTTPException(status_code=REACHED, detail="reached")

    service = SimpleNamespace(get_node_names=_node_names)
    return {
        "deregister": lambda: cluster_registration.delete_cluster(cluster, cert, db, None, user),
        "regenerate-certificate": lambda: cluster_registration.regenerate_certificate(cluster, cert, db, None, user),
        "move": lambda: cluster_registration.update_cluster_location(
            cluster, ClusterLocationUpdateRequest(location=JAKARTA), cert, None, user, db),
        "rename": lambda: nodes.update_node(cluster, NodeUpdateRequest(display_name="x"), service, None, user, db),
    }


ROUTES = ["deregister", "regenerate-certificate", "move", "rename"]


@pytest.mark.parametrize("cluster", [MIXED, SOLO], ids=["held-beside-another-party", "held-by-another-party-alone"])
@pytest.mark.parametrize("route", ROUTES)
async def test_a_cluster_another_party_holds_is_not_changed_for_everyone(scope, reached, route: str, cluster: str) -> None:
    db = await _world()
    with pytest.raises(HTTPException) as caught:
        await _routes(cluster, _user(SELLER), db, reached)[route]()
    assert caught.value.status_code == 403 and caught.value.detail == SHARED_CLUSTER_DETAIL
    assert reached == []


@pytest.mark.parametrize("route", ROUTES)
async def test_the_whole_holder_and_an_admin_still_change_it(scope, reached, route: str) -> None:
    db = await _world()
    for cluster, user in ((OWN, _user(SELLER)), (SOLO, _user(ADMIN, "role_admin"))):
        try:
            await _routes(cluster, user, db, reached)[route]()
        except HTTPException as exc:
            assert exc.status_code != 403, exc.detail
    assert reached[:1] == [OWN] and SOLO in reached


@pytest.mark.parametrize("owner", ["default", None])
async def test_a_default_project_network_write_needs_the_whole_cluster(scope, owner) -> None:
    db = await _world()
    with pytest.raises(HTTPException) as caught:
        await verify_default_project_write(MIXED, owner, db, _user(SELLER))
    assert caught.value.status_code == 403
    await verify_default_project_write(OWN, owner, db, _user(SELLER))
    await verify_default_project_write(MIXED, owner, db, _user(ADMIN, "role_admin"))


async def test_a_network_in_ones_own_project_is_not_held_to_the_whole_cluster(scope) -> None:
    db = await _world()
    await verify_default_project_write(MIXED, "own-web", db, _user(SELLER))


def test_every_network_and_acl_write_asks_after_resolving_the_owner() -> None:
    for module, writes in ((networks, ("create_network", "update_network", "delete_network")),
                           (network_acls, ("create_network_acl", "update_network_acl", "delete_network_acl"))):
        for name in writes:
            source = inspect.getsource(getattr(module, name))
            assert "await verify_default_project_write(host, owner, db, _rbac)" in source, name
            assert source.index("project_is_visible(host, owner)") < source.index("verify_default_project_write"), name


# ── a warning is dismissed by the project it belongs to ────────────────────────────────────────

class _Warnings:
    def __init__(self, project: str) -> None:
        self.project, self.deleted = project, []

    def __getitem__(self, uuid: str) -> Any:
        owner = self

        class _One:
            def get(self, timeout: Any = None) -> Any:
                return SimpleNamespace(status_code=200, json=lambda: {"metadata": {"uuid": uuid, "project": owner.project}})

            def delete(self, timeout: Any = None) -> Any:
                owner.deleted.append(uuid)
                return SimpleNamespace(status_code=200)

        return _One()


@pytest.mark.parametrize("cluster,project,expected", [
    (SOLO, "", 403), (MIXED, "", 403), (MIXED, "theirs", 404),
], ids=["server-scope-where-another-party-holds-alone", "server-scope-beside-another-party",
        "another-partys-project"])
async def test_a_warning_outside_ones_reach_is_not_dismissed(scope, monkeypatch, cluster, project, expected) -> None:
    from app.routers import lxd_warnings

    db, warnings = await _world(), _Warnings(project)

    async def _client(_service: Any, _host: str) -> Any:
        return SimpleNamespace(api=SimpleNamespace(warnings=warnings))

    monkeypatch.setattr(lxd_warnings, "client_for", _client)
    with pytest.raises(HTTPException) as caught:
        await lxd_warnings.delete_warning("w1", cluster, None, None, _user(SELLER), db)
    assert caught.value.status_code == expected and warnings.deleted == []


async def test_a_warning_in_ones_own_reach_is_dismissed(scope, monkeypatch) -> None:
    from app.routers import lxd_warnings

    db, warnings = await _world(), _Warnings("")

    async def _client(_service: Any, _host: str) -> Any:
        return SimpleNamespace(api=SimpleNamespace(warnings=warnings))

    monkeypatch.setattr(lxd_warnings, "client_for", _client)
    monkeypatch.setattr(lxd_warnings, "get_cache_manager", lambda: SimpleNamespace(invalidate_resource_cache=lambda *a, **k: None))
    await lxd_warnings.delete_warning("w1", OWN, None, None, _user(SELLER), db)
    assert warnings.deleted == ["w1"]


# ── tag authorship and visibility are the verified caller's ─────────────────────────────────────

def test_tags_read_the_verified_caller_not_a_header() -> None:
    from app.routers import tags

    assert tags.get_user_id(_user(SELLER)) == SELLER
    assert "Header" not in inspect.getsource(tags.get_user_id)


# ── an orphaned registration is an admin's to claim ────────────────────────────────────────────

@pytest.mark.parametrize("registered,expected", [({"orphan"}, 403), (None, 503)], ids=["orphan", "registry-unreadable"])
async def test_an_orphaned_registration_is_not_claimed_by_a_partner(monkeypatch, registered, expected) -> None:
    from app.services.lxd import registry

    async def _registered(fresh: bool = False) -> Any:
        return frozenset(registered) if registered is not None else None

    monkeypatch.setattr(registry, "registered_cluster_ids", _registered)
    db = await _world()
    with pytest.raises(HTTPException) as caught:
        await cluster_registration._guard_registration_binding(db, "orphan", _user(SELLER))
    assert caught.value.status_code == expected


async def test_a_new_cluster_and_an_admin_claim_still_register(monkeypatch) -> None:
    from app.services.lxd import registry

    async def _registered(fresh: bool = False) -> Any:
        return frozenset({"orphan"})

    monkeypatch.setattr(registry, "registered_cluster_ids", _registered)
    db = await _world()
    await cluster_registration._guard_registration_binding(db, "brand-new", _user(SELLER))
    await cluster_registration._guard_registration_binding(db, "orphan", _user(ADMIN, "role_admin"))
