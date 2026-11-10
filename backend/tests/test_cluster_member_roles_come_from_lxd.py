"""A cluster member's roles are the array LXD reports, never derived from its database flag.

Deriving them from the flag names every database member the leader. GET
/cluster-members/{member} returns LXD's own roles array, as the list endpoint does.
"""

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import cluster_members
from app.services.lxd_service import get_lxd_service
from app.core.org_scope import org_scope_dependency
from app.core.rbac import get_current_user


class _Response:
    def __init__(self, body):
        self._body = body

    def json(self):
        return {"metadata": self._body}


class _Members(dict):
    def __getitem__(self, name):
        body = dict.__getitem__(self, name)
        return SimpleNamespace(get=lambda: _Response(body))


def _client(members):
    api = SimpleNamespace(cluster=SimpleNamespace(members=_Members(members)))
    return SimpleNamespace(api=api)


def _app(members) -> TestClient:
    svc = SimpleNamespace(
        connection_manager=SimpleNamespace(get_client=lambda node: _client(members)),
    )
    app = FastAPI()
    app.include_router(cluster_members.router)
    app.dependency_overrides[get_lxd_service] = lambda: svc
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


MEMBERS = {
    "mc1": {"server_name": "mc1", "database": True, "roles": ["database-leader", "database"],
            "status": "Online", "url": "https://10.0.0.11:8443"},
    "mc2": {"server_name": "mc2", "database": True, "roles": ["database"],
            "status": "Online", "url": "https://10.0.0.12:8443"},
    "mc3": {"server_name": "mc3", "database": False, "roles": [],
            "status": "Online", "url": "https://10.0.0.13:8443"},
}


def test_a_database_member_is_not_called_the_leader() -> None:
    client = _app(MEMBERS)
    got = {name: client.get(f"/cluster-members/{name}", params={"node": "mc-lab"}).json()["roles"]
           for name in MEMBERS}
    assert got == {
        "mc1": ["database-leader", "database"],
        "mc2": ["database"],
        "mc3": [],
    }, "roles must be the array LXD reports, as the list endpoint already returns"


def test_a_member_lxd_reports_without_roles_has_none() -> None:
    client = _app({"m": {"server_name": "m", "database": True}})
    body = client.get("/cluster-members/m", params={"node": "mc-lab"}).json()
    assert body["roles"] == []
    assert body["database"] is True
