"""
A name that is empty, `.` or `..` is refused with 400 before any cluster call, on every key door.

requests folds a `.` or `..` segment out of the path it sends, so such a name addresses the object's
parent: `..` as an instance name reads or deletes the collection, or the server root. Round 1d holds
every caller-supplied name in a key-reachable module to lxd_segment. Each case sends one request
through the real route (tests/_guest_gate_doors.py) and asserts a 400 naming the field, no client
built, nothing sent and no failure record (a dot-name 400 is not audited, like the raw proxy's
traversal 400). Covered: every /proxy operation that puts a name into a path, the MCP tools that
call them, GET /integrations/operations/{id}, and every read a project-limited key's write plan
makes (profiles, a copy source and its snapshot, an image fingerprint or alias, a restore) on the
raw proxy, /proxy and MCP.

A case marked xfail(strict=True) is a known defect. It asserts what the rule requires and turns red
once the product is fixed, so the marker comes off with the fix.
"""

from typing import Any, Dict, List, Tuple

import pytest
import requests
from fastapi import HTTPException
from pylxd.client import _APINode

from app.mcp import tools_admin, tools_desktop, tools_guest, tools_inventory, tools_read, tools_write
from app.routers import integrations
from app.services.lxd.proxy_operations import PROXY_OPERATION_LEVELS
from app.services.lxd.proxy_path_params import PATH_PARAMS
from app.services.lxd.utils import find_instance_in_projects
from tests._cloud_init_fakes import ConfigSession
from tests._guest_gate_doors import (  # noqa: F401
    EVERY, NARROW, TWO, Cluster, SnapshotClient, door_fixture,
)
from tests._limited_write_fakes import FakeClient, ProjectSession, _no_webhook_events  # noqa: F401
from tests._raw_proxy_fakes import CLUSTER_ID, ENDPOINT, _clean_breaker, fence_calls  # noqa: F401

DOTS = ["", ".", ".."]
OP_ID = "5b1e0a4c-0000-4000-8000-000000000001"
# note: every other param each operation needs, so only the named field is wrong.
BASE_PARAMS: Dict[str, Dict[str, Any]] = {
    "create_instance": {"name": "c1", "source": {"type": "none"}},
    "update_instance": {"name": "web", "config": {"limits.cpu": "2"}},
    "create_snapshot": {"name": "web", "snapshot_name": "s0"},
    "restore_snapshot": {"name": "web", "snapshot_name": "s0"},
    "delete_snapshot": {"name": "web", "snapshot_name": "s0"},
    "exec_instance": {"name": "web", "command": ["id"]},
    "get_file": {"name": "web", "path": "/etc/hostname"},
    "put_file": {"name": "web", "path": "/tmp/x", "content": "x"},
    "desktop_view": {"name": "web", "action": "screenshot"},
    "desktop_input": {"name": "web", "action": "left_click", "coordinate": [1, 1]},
    "desktop_clipboard": {"name": "web", "action": "read"},
    "get_operation": {"operation_id": OP_ID},
    "create_storage_pool": {"definition": {"name": "p1", "driver": "dir"}},
}
# note: every /proxy operation that puts a caller's name into an LXD path (services/lxd/proxy_operations.py),
# written out here rather than read from the gate's own table, so a name dropped from the table fails a case.
NAMED = {
    **{operation: ("name",) for operation in (
        "get_instance", "create_instance", "update_instance", "start_instance", "stop_instance", "restart_instance",
        "delete_instance", "list_snapshots", "create_snapshot", "restore_snapshot", "exec_instance", "get_file",
        "put_file", "desktop_probe", "desktop_view", "desktop_input", "desktop_clipboard", "create_network",
        "delete_network", "create_storage_pool", "delete_storage_pool", "create_project", "delete_project")},
    "delete_snapshot": ("name", "snapshot_name"),
    "get_operation": ("operation_id",),
}
PROXY_CASES: List[Tuple[str, str, Any]] = [
    (operation, field, value) for operation, fields in NAMED.items() for field in fields for value in DOTS
] + [("create_storage_pool", "top-level name", value) for value in DOTS]


def test_every_named_operation_is_in_the_gate_and_the_vocabulary() -> None:
    assert set(NAMED) == set(PATH_PARAMS) and set(NAMED) <= set(PROXY_OPERATION_LEVELS)
    assert sorted(op for op in PROXY_OPERATION_LEVELS if op not in NAMED) == [
        "list_images", "list_instances", "list_networks", "list_profiles", "list_projects", "list_storage_pools"]


def _params(operation: str, field: str, value: Any) -> Dict[str, Any]:
    params = {**BASE_PARAMS.get(operation, {"name": "web"})}
    if operation == "create_storage_pool":
        return {"name": value, "driver": "dir"} if field == "top-level name" else {"definition": {"name": value,
                                                                                                    "driver": "dir"}}
    return {**params, field: value}


def _refused(seen: Any) -> None:
    assert seen.status in (400, 0), (seen.status, seen.text)
    assert "cannot name an LXD object" in seen.text or "is required" in seen.text, seen.text
    assert seen.cluster.untouched, (seen.cluster.built, seen.cluster.session.calls)
    assert seen.refusals == []


@pytest.mark.parametrize("operation,field,value", PROXY_CASES, ids=[f"{o}-{f}-{v!r}" for o, f, v in PROXY_CASES])
def test_proxy_refuses_a_dot_name_before_any_client(door: Any, operation: str, field: str, value: str) -> None:
    _refused(door.proxy(EVERY, operation, _params(operation, field, value)))


@pytest.mark.parametrize("value", [".", ".."])
def test_proxy_refuses_a_dot_snapshot_name_on_create(door: Any, value: str) -> None:
    seen = door.proxy(EVERY, "create_snapshot", {"name": "web", "snapshot_name": value},
                      cluster=Cluster(client=SnapshotClient()))
    _refused(seen)
    assert seen.cluster.client.sent == []


def test_proxy_still_creates_a_named_snapshot(door: Any) -> None:
    seen = door.proxy(EVERY, "create_snapshot", {"name": "web", "snapshot_name": "snap-1"},
                      cluster=Cluster(client=SnapshotClient()))
    assert seen.status == 200 and seen.cluster.client.sent == [("snapshot", "web", "snap-1")], seen.text


MCP_CASES = [
    (tools_read, "lxd_get_instance", {}), (tools_read, "lxd_list_snapshots", {}),
    (tools_write, "lxd_create_instance", {}), (tools_write, "lxd_update_instance", {"config": {"limits.cpu": "2"}}),
    (tools_write, "lxd_start_instance", {}), (tools_write, "lxd_stop_instance", {}),
    (tools_write, "lxd_restart_instance", {}), (tools_write, "lxd_create_snapshot", {"snapshot_name": "s0"}),
    (tools_write, "lxd_restore_snapshot", {"snapshot_name": "s0"}), (tools_admin, "lxd_delete_instance", {}),
    (tools_admin, "lxd_delete_snapshot", {"snapshot_name": "s0"}), (tools_admin, "lxd_create_network", {}),
    (tools_admin, "lxd_delete_network", {}), (tools_admin, "lxd_create_storage_pool", {"driver": "dir"}),
    (tools_admin, "lxd_delete_storage_pool", {}), (tools_admin, "lxd_create_project", {}),
    (tools_admin, "lxd_delete_project", {}), (tools_guest, "lxd_exec_instance", {"command": ["id"]}),
    (tools_guest, "lxd_read_file", {"path": "/etc/hostname"}), (tools_guest, "lxd_write_file", {"path": "/tmp/x",
                                                                                                 "content": "x"}),
    (tools_desktop, "desktop_probe", {}), (tools_desktop, "desktop_screenshot", {}),
    (tools_desktop, "desktop_read_clipboard", {}),
]


@pytest.mark.parametrize("value", [".", ".."])
@pytest.mark.parametrize("module,tool,extra", MCP_CASES, ids=[tool for _m, tool, _e in MCP_CASES])
async def test_mcp_refuses_a_dot_name_before_any_client(door: Any, module: Any, tool: str, extra: Dict[str, Any],
                                                        value: str) -> None:
    _refused(await door.mcp(module, tool, EVERY, cluster_id=CLUSTER_ID, name=value, **extra))


@pytest.mark.parametrize("value", [".", ".."])
async def test_mcp_refuses_a_dot_snapshot_on_delete_and_a_dot_operation(door: Any, value: str) -> None:
    _refused(await door.mcp(tools_admin, "lxd_delete_snapshot", EVERY, cluster_id=CLUSTER_ID, name="web",
                            snapshot_name=value))
    _refused(await door.mcp(tools_inventory, "orcastra_get_operation", EVERY, cluster_id=CLUSTER_ID,
                            operation_id=value))


@pytest.mark.parametrize("spelled", ["%2E", "%2E%2E", "%2e%2e"])
def test_the_operations_route_refuses_a_dot_id(door: Any, spelled: str) -> None:
    seen = door.get(EVERY, f"/api/v1/integrations/operations/{spelled}", project="default")
    assert seen.status == 400 and "cannot name an LXD object" in seen.text and seen.cluster.untouched


# note: a project-limited key's write plan

PLAN_CASES = [
    ("POST", "/1.0/instances", "tenant-a", {"name": "c1", "source": {"type": "none"}, "profiles": ["@"]}),
    ("POST", "/1.0/instances", "tenant-a", {"name": "c1", "source": {"type": "none"}, "profiles": ["default", "@"]}),
    ("POST", "/1.0/instances", "tenant-a", {"name": "c1", "source": {"type": "copy", "source": "@"}}),
    ("POST", "/1.0/instances", "tenant-a", {"name": "c1", "source": {"type": "copy", "source": "web/@"}}),
    ("POST", "/1.0/instances", "tenant-a", {"name": "c1", "source": {"type": "image", "fingerprint": "@"}}),
    ("POST", "/1.0/instances", "tenant-a", {"name": "c1", "source": {"type": "image", "alias": "@"}}),
    ("PUT", "/1.0/instances/web", "tenant-a", {"restore": "@"}),
    ("PATCH", "/1.0/instances/web", "tenant-a", {"profiles": ["@"]}),
    ("PUT", "/1.0/instances/web", "tenant-a", {"config": {}, "devices": {}, "profiles": ["@"]}),
    ("POST", "/1.0/instances/web", "tenant-a", {"name": "web", "project": "tenant-b", "profiles": ["@"]}),
    ("POST", "/1.0/images", "tenant-a", {"source": {"type": "instance", "name": "web"}, "profiles": ["@"]}),
    ("PATCH", "/1.0/%69nstances/web/", "tenant-a", {"profiles": ["@"]}),
]
PLAN_IDS = ["profile", "second-profile", "copy-source", "copy-snapshot", "image-fingerprint", "image-alias", "restore",
            "patch-profiles", "put-profiles", "move-profiles", "image-profiles", "encoded-path"]


def _dotted(body: Any, value: str) -> Any:
    if isinstance(body, dict):
        return {k: _dotted(v, value) for k, v in body.items()}
    if isinstance(body, list):
        return [_dotted(v, value) for v in body]
    return body.replace("@", value) if isinstance(body, str) else body


@pytest.mark.parametrize("value", [".", ".."])
@pytest.mark.parametrize("method,path,project,body", PLAN_CASES, ids=PLAN_IDS)
def test_a_limited_plan_read_refuses_a_dot_name_before_any_client(door: Any, method: str, path: str, project: str,
                                                                  body: Dict[str, Any], value: str) -> None:
    seen = door.raw(TWO, method, path, body=_dotted(body, value), project=project, cluster=Cluster(ProjectSession()))
    _refused(seen)


PROXY_PLAN = [
    ("create_instance", {"name": "c1", "source": {"type": "none"}, "profiles": ["@"]}),
    ("create_instance", {"name": "c1", "source": {"type": "copy", "source": "@"}}),
    ("create_instance", {"name": "c1", "source": {"type": "copy", "source": "web/@"}}),
    ("create_instance", {"name": "c1", "source": {"type": "image", "fingerprint": "@"}}),
    ("create_instance", {"name": "c1", "source": {"type": "image", "alias": "@"}}),
    ("update_instance", {"name": "web", "profiles": ["@"]}),
    ("restore_snapshot", {"name": "web", "snapshot_name": "@"}),
]


@pytest.mark.parametrize("value", [".", ".."])
@pytest.mark.parametrize("operation,params", PROXY_PLAN, ids=[f"{o}-{i}" for i, (o, _p) in enumerate(PROXY_PLAN)])
def test_a_limited_proxy_plan_refuses_a_dot_name_before_any_client(door: Any, operation: str, params: Dict[str, Any],
                                                                   value: str) -> None:
    seen = door.proxy(NARROW, operation, _dotted(params, value), project="tenant-a",
                      cluster=Cluster(client=FakeClient(ProjectSession())))
    _refused(seen)


@pytest.mark.parametrize("value", [".", ".."])
async def test_a_limited_mcp_plan_refuses_a_dot_name_before_any_client(door: Any, value: str) -> None:
    for tool, extra in (("lxd_create_instance", {"name": "c1", "profiles": [value]}),
                        ("lxd_update_instance", {"name": "web", "profiles": [value]}),
                        ("lxd_restore_snapshot", {"name": "web", "snapshot_name": value})):
        _refused(await door.mcp(tools_write, tool, NARROW, Cluster(client=FakeClient(ProjectSession())),
                                cluster_id=CLUSTER_ID, project="tenant-a", **extra))


# note: the one-instance read, which MCP's orcastra_get_instance calls


class _LxdRoot(ConfigSession):
    """Prepares each URL the way requests does and answers the server root, as LXD would, with its config."""

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        prepared = requests.Request("GET", url, params=kwargs.get("params")).prepare().url
        self.calls.append(("GET", prepared, kwargs))
        path = prepared.split("/1.0", 1)[1].split("?", 1)[0]
        response = requests.Response()
        root = {"type": "sync", "metadata": {"config": {"core.https_address": "10.0.0.9:8443"}}}
        missing = {"type": "error", "error": "Not Found", "error_code": 404}
        response.status_code, body = (200, root) if path in ("", "/") else (404, missing)
        response._content = requests.compat.json.dumps(body).encode()
        return response


def _root_cluster(monkeypatch: pytest.MonkeyPatch) -> Cluster:
    cluster = Cluster(_LxdRoot())
    cluster.client = type("Client", (), {})()
    cluster.client.api = _APINode(ENDPOINT, session=cluster.session, timeout=(3, 8))
    monkeypatch.setattr(integrations, "connect_or_get_client", lambda _svc, _cid: cluster.client)
    return cluster


@pytest.mark.parametrize("api_key,project", [(NARROW, "tenant-a"), (EVERY, None)], ids=["narrow", "every-project"])
@pytest.mark.parametrize("spelled", ["%2E%2E", "%2E", "%2e%2e"])
def test_the_instance_read_refuses_a_dot_name(door: Any, monkeypatch: pytest.MonkeyPatch, spelled: str,
                                              api_key: Dict[str, Any], project: Any) -> None:
    cluster = _root_cluster(monkeypatch)
    params = {} if project is None else {"project": project}
    seen = door.get(api_key, f"/api/v1/integrations/instances/{spelled}", cluster=cluster, **params)
    assert seen.status == 400 and "cannot name an LXD object" in seen.text, (seen.status, seen.text)
    assert cluster.session.calls == [] and seen.refusals == []


@pytest.mark.parametrize("value", DOTS)
async def test_mcp_instance_read_refuses_a_dot_name(door: Any, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    cluster = _root_cluster(monkeypatch)
    seen = await door.mcp(tools_inventory, "orcastra_get_instance", NARROW, cluster, name=value, cluster_id=CLUSTER_ID,
                          project="tenant-a")
    assert seen.status == 0 and "cannot name an LXD object" in seen.text, seen.text
    assert cluster.session.calls == []


def test_the_instance_read_still_answers_a_real_name(door: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = _root_cluster(monkeypatch)
    seen = door.get(NARROW, "/api/v1/integrations/instances/web", cluster=cluster, project="tenant-a")
    assert seen.status == 404 and [url for _verb, url, _kw in cluster.session.calls] == [
        f"{ENDPOINT}/instances/web?project=tenant-a"]


class _Projects:
    """A pylxd client's project list and instance node, recording every request either makes."""

    def __init__(self, calls: List[str]) -> None:
        self.calls = calls

    def all(self) -> List[Any]:
        self.calls.append("projects.all")
        return [type("P", (), {"name": "default"})()]


@pytest.mark.parametrize("project", [None, "tenant-a"], ids=["every-project", "one-project"])
@pytest.mark.parametrize("value", DOTS)
def test_the_shared_instance_lookup_refuses_a_dot_name_before_any_request(value: str, project: Any) -> None:
    calls: List[str] = []
    client = type("Client", (), {})()
    client.projects = _Projects(calls)
    client.api = _APINode(ENDPOINT, session=_LxdRoot(), timeout=(3, 8))
    with pytest.raises(HTTPException) as refused:
        find_instance_in_projects(client, value, project)
    assert refused.value.status_code == 400 and "Instance name" in str(refused.value.detail)
    assert calls == [] and client.api.session.calls == []

