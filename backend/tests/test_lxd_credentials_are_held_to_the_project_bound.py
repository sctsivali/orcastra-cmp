"""An LXD credential minted or rewired through the dashboard is held to the caller's project bound.

My Keys checked only the cluster, so on a cluster bound to two organizations a Partner could
mint a server `admin` key, or a standard key that is operator on the other organization's
project. The Groups and Identities routers let the same Partner write any permission list into
an LXD auth group, put any identity into any group, or delete any identity. LXD auth objects
belong to no project, so on a shared cluster only an admin, who has no bound, may write them.

The scan at the end pins the class: a route that checks a cluster and takes a project must also
hold that project to the bound.
"""

import ast
from pathlib import Path
from typing import Any, Callable

import pytest
from fastapi import HTTPException

from app.routers import my_keys, permissions
from tests.instance_access_fakes import CLUSTER, partner, scoped

FOREIGN = "other-db"
ROUTERS = Path(__file__).resolve().parents[1] / "app" / "routers"


class _Reached(Exception):
    """Raised by the fake LXD service: the handler got past its authorization."""


class _Service:
    def __init__(self) -> None:
        self.asked: list[str] = []

        def get_client(host: str) -> None:
            self.asked.append(host)
            raise _Reached(host)

        self.connection_manager = type("CM", (), {"get_client": staticmethod(get_client)})()


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> _Service:
    monkeypatch.setattr(my_keys, "get_vault_keys_client", lambda: object())
    return _Service()


def _key(level: str, project: str) -> my_keys.KeyCreateRequest:
    return my_keys.KeyCreateRequest(name="probe", node=CLUSTER, project=project, auth_level=level)


async def _create(service: _Service, level: str, project: str) -> Any:
    return await my_keys.create_key(
        request=_key(level, project), lxd_service=service, _=None, _rbac=None
    )


# -- My Keys -----------------------------------------------------------------------------------

async def test_a_server_admin_key_on_a_shared_cluster_is_refused(service: _Service) -> None:
    with partner({"default", "own-web"}):
        with pytest.raises(HTTPException) as exc:
            await _create(service, "admin", "default")
    assert exc.value.status_code == 403
    assert service.asked == []


async def test_a_standard_key_on_a_foreign_project_is_refused(service: _Service) -> None:
    with partner({"default", "own-web"}):
        with pytest.raises(HTTPException) as exc:
            await _create(service, "standard", FOREIGN)
    assert exc.value.status_code == 403
    assert service.asked == []


async def test_a_standard_key_on_an_own_project_is_minted(service: _Service) -> None:
    with partner({"default", "own-web"}):
        with pytest.raises(HTTPException) as exc:
            await _create(service, "standard", "own-web")
    assert exc.value.status_code == 500
    assert service.asked == [CLUSTER]


async def test_an_admin_key_on_an_unshared_cluster_is_still_minted(service: _Service) -> None:
    with scoped(allowed={CLUSTER}):
        with pytest.raises(HTTPException) as exc:
            await _create(service, "admin", "default")
    assert exc.value.status_code == 500
    assert service.asked == [CLUSTER]


# -- Groups and Identities ---------------------------------------------------------------------

def _create_group(service: _Service) -> Any:
    return permissions.create_group(
        name="g", description="", permissions=[{"entity_type": "server", "url": "/1.0",
                                                "entitlement": "admin"}],
        host=CLUSTER, lxd_service=service, _=None,
    )


def _update_group(service: _Service) -> Any:
    return permissions.update_group(
        group_name="g", request=permissions.GroupUpdateRequest(permissions=[]),
        host=CLUSTER, lxd_service=service, _=None,
    )


def _delete_group(service: _Service) -> Any:
    return permissions.delete_group(group_name="g", host=CLUSTER, lxd_service=service, _=None)


def _add_identity(service: _Service) -> Any:
    return permissions.add_identity_to_group(
        group_name="g", request=permissions.IdentityAddRequest(auth_type="tls", identity_id="i"),
        host=CLUSTER, lxd_service=service, _=None,
    )


def _remove_identity(service: _Service) -> Any:
    return permissions.remove_identity_from_group(
        group_name="g", auth_type="tls", identity_id="i", host=CLUSTER, lxd_service=service, _=None,
    )


def _delete_identity(service: _Service) -> Any:
    return permissions.delete_identity(
        auth_type="tls", identity_id="i", host=CLUSTER, lxd_service=service, _=None
    )


WRITES: list[Callable[[_Service], Any]] = [
    _create_group, _update_group, _delete_group, _add_identity, _remove_identity, _delete_identity,
]
IDS = ["create_group", "update_group", "delete_group", "add_identity", "remove_identity",
       "delete_identity"]


@pytest.mark.parametrize("write", WRITES, ids=IDS)
async def test_an_auth_write_on_a_shared_cluster_is_refused(write, service: _Service) -> None:
    with partner({"default", "own-web"}):
        with pytest.raises(HTTPException) as exc:
            await write(service)
    assert exc.value.status_code == 403
    assert service.asked == []


@pytest.mark.parametrize("write", WRITES, ids=IDS)
async def test_an_auth_write_on_an_unshared_cluster_goes_through(write, service: _Service) -> None:
    with scoped(allowed={CLUSTER}):
        with pytest.raises(HTTPException) as exc:
            await write(service)
    assert exc.value.status_code == 500
    assert service.asked == [CLUSTER]


def test_every_auth_write_route_checks_the_whole_cluster() -> None:
    tree = ast.parse((ROUTERS / "permissions.py").read_text())
    writes = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and any(isinstance(d, ast.Call) and getattr(d.func, "attr", "") in {"post", "patch", "put", "delete"}
                for d in node.decorator_list)
    ]
    assert len(writes) == len(WRITES)
    for node in writes:
        assert "verify_whole_cluster_access(" in ast.unparse(node), node.name


# -- the class: a cluster check with a project and no project bound ----------------------------

PROJECT_GUARDS = {
    "verify_project_access", "verify_instance_view_access", "resolve_instance_project",
    "project_is_visible", "can_view_instance", "verify_whole_cluster_access",
    "_authorize_tag_target",
}
EXEMPT = {
    ("iso_uploads_dashboard.py", "create_upload"): "refuses every project except default",
}
ROUTE_VERBS = {"get", "post", "put", "patch", "delete", "websocket"}


def _called(node: ast.AST) -> set[str]:
    names = set()
    for call in ast.walk(node):
        if isinstance(call, ast.Call):
            func = call.func
            names.add(func.id if isinstance(func, ast.Name) else getattr(func, "attr", ""))
    return names


def test_every_route_that_takes_a_project_holds_it_to_the_bound() -> None:
    unguarded = []
    for path in sorted(ROUTERS.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.AsyncFunctionDef) or not any(
                isinstance(d, ast.Call) and getattr(d.func, "attr", "") in ROUTE_VERBS
                for d in node.decorator_list
            ):
                continue
            called = _called(node)
            if "verify_cluster_access" not in called:
                continue
            params = {a.arg for a in node.args.args + node.args.kwonlyargs}
            takes_project = bool(params & {"project", "project_name"}) or ".project" in ast.unparse(node)
            if takes_project and not called & PROJECT_GUARDS and (path.name, node.name) not in EXEMPT:
                unguarded.append(f"{path.name}:{node.lineno} {node.name}")
    assert unguarded == [], unguarded
