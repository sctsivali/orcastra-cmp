"""The member dialog folds shared pools once, reads instances in one request, and never
presents a failed read as zero.

Seen in production on a three member MicroCloud with MicroCeph: every member reported
Storage 6.76 TB / 12.37 TB, which is local 1.7 TB plus the two cluster-wide ceph pools
(5.3 TB each) that every member sees, and Instances "0 / 0 total" with CPU "0 / 88 cores"
after the instance read timed out, cached for the full TTL with the partial flag the modal
never rendered.

Hermetic: a fake client shaped like the real daemon's answers, the reader driven directly,
and the route driven through the app with a cache spy.
"""

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import cluster_members
from app.routers.cluster_members import (
    DEGRADED_CACHE_TTL,
    _read_member_resources,
    count_member_instances,
    parse_cpu_limit,
)
from app.services.lxd.lxd_service import LXDService

TIB = 1024 ** 4
GIB = 1024 ** 3
NODE = "C2"
MEMBERS = ["microcloud20", "microcloud30", "microcloud40"]


class _Response:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _Endpoint:
    def __init__(self, handler) -> None:
        self._handler = handler

    def get(self, params: Dict[str, Any] = None, timeout: Any = None) -> _Response:
        return _Response(self._handler(params or {}))


class _Pools:
    def __init__(self, pools, space, calls: List[str]):
        self._pools, self._space, self.calls = pools, space, calls

    def get(self, params: Dict[str, Any] = None, timeout: Any = None) -> _Response:
        self.calls.append(f"pools recursion={params.get('recursion')}")
        return _Response({"metadata": self._pools})

    def __getitem__(self, name: str):
        def _space(params):
            member = params["target"]
            self.calls.append(f"{name}@{member}")
            if member not in self._space[name]:
                raise RuntimeError("member did not answer")
            return {"metadata": {"space": self._space[name][member]}}

        return SimpleNamespace(resources=_Endpoint(_space))


class _Instances:
    def __init__(self, rows, calls: List[str], fail: bool = False):
        self._rows, self.calls, self._fail = rows, calls, fail

    def get(self, params: Dict[str, Any] = None, timeout: Any = None) -> _Response:
        self.calls.append(f"instances recursion={params.get('recursion')} all-projects={params.get('all-projects')}")
        if self._fail:
            raise TimeoutError("read timed out")
        return _Response({"metadata": self._rows})


POOLS = [
    {"name": "local", "driver": "zfs", "status": "Created", "locations": MEMBERS},
    {"name": "remote", "driver": "ceph", "status": "Created", "locations": MEMBERS},
    {"name": "sata-ssd", "driver": "ceph", "status": "Created", "locations": MEMBERS},
]
SPACE = {
    "local": {m: {"total": int(1.7 * TIB), "used": 131 * GIB} for m in MEMBERS},
    "remote": {m: {"total": int(5.3 * TIB), "used": int(3.1 * TIB)} for m in MEMBERS},
    "sata-ssd": {m: {"total": int(5.3 * TIB), "used": int(3.5 * TIB)} for m in MEMBERS},
}
INSTANCES = [
    {"name": "web01", "project": "atlas-desktop-lab", "location": "microcloud20", "status": "Running",
     "config": {"limits.cpu": "4"}, "expanded_config": {"limits.cpu": "4"}, "expanded_devices": {}},
    {"name": "db01", "project": "atlas-desktop-lab", "location": "microcloud20", "status": "Running",
     "config": {}, "expanded_config": {}, "expanded_devices": {}},
    {"name": "old", "project": "default", "location": "microcloud20", "status": "Stopped",
     "config": {"limits.cpu": "0-3"}, "expanded_config": {}, "expanded_devices": {}},
    {"name": "elsewhere", "project": "default", "location": "microcloud30", "status": "Running",
     "config": {"limits.cpu": "0,2"}, "expanded_config": {}, "expanded_devices": {}},
]


class FakeClient:
    project = None

    def __init__(self, instances_fail: bool = False, pools=POOLS, space=SPACE):
        self.calls: List[str] = []
        self.host_info = {
            "environment": {"server_clustered": True, "server_name": MEMBERS[0]},
            "api_extensions": ["instance_all_projects"],
        }
        per_member = {"cpu": {"total": 88}, "memory": {"total": 256 * GIB, "used": 90 * GIB}}
        self.api = _API(
            resources=_Endpoint(lambda p: {"metadata": per_member}),
            storage_pools=_Pools(pools, space, self.calls),
            instances=_Instances(INSTANCES, self.calls, fail=instances_fail),
        )


class _API(SimpleNamespace):
    """pylxd's api node answers both `api.instances` and `api["instances"]`."""

    def __getitem__(self, item: str):
        return getattr(self, item.replace("-", "_"))


class _Manager:
    def __init__(self, client):
        self._client = client

    def get_client(self, host, *args, **kwargs):
        return self._client

    def get_node_names(self):
        return [NODE]


def _service(client) -> LXDService:
    svc = LXDService.__new__(LXDService)
    svc.connection_manager = _Manager(client)
    svc.cert_manager = None
    return svc


def test_shared_pools_are_counted_once_and_marked_and_local_is_the_member_s_own():
    client = FakeClient()
    res = _read_member_resources(_service(client), NODE, "microcloud20")
    by_name = {p["name"]: p for p in res.storage_pools}
    assert by_name["local"]["scope"] == "local" and by_name["local"]["members"] == ["microcloud20"]
    assert by_name["remote"]["scope"] == "shared" and by_name["remote"]["members"] == MEMBERS
    assert by_name["sata-ssd"]["scope"] == "shared"
    assert res.storage_local_total == int(1.7 * TIB) and res.storage_local_used == 131 * GIB
    assert res.storage_shared_total == 2 * int(5.3 * TIB)
    assert res.storage_total == sum(p["space_total"] for p in res.storage_pools)
    assert res.storage_total == res.storage_local_total + res.storage_shared_total
    assert res.storage_partial is False
    assert client.calls.count("pools recursion=1") == 1, "one pool list, no per-pool detail read"
    assert not any(c.startswith("pools recursion=0") for c in client.calls)


def test_instances_are_counted_by_location_from_one_recursion_1_read():
    client = FakeClient()
    res = _read_member_resources(_service(client), NODE, "microcloud20")
    assert [c for c in client.calls if c.startswith("instances")] == ["instances recursion=1 all-projects=true"]
    assert (res.instances_count, res.instances_running) == (3, 2)
    # 4 cores for web01, 1 for db01 with no limit, the stopped one allocates nothing
    assert res.cpu_used == 5 and res.cpu_total == 88
    assert res.instances_partial is False


def test_a_failed_instance_read_is_marked_and_not_rendered_as_zero_capacity():
    client = FakeClient(instances_fail=True)
    res = _read_member_resources(_service(client), NODE, "microcloud20")
    assert res.instances_partial is True
    assert (res.instances_count, res.instances_running, res.cpu_used) == (0, 0, 0)
    # the storage read is independent and still complete
    assert res.storage_partial is False and res.storage_total > 0


def test_a_pool_a_member_did_not_answer_for_marks_storage_partial():
    space = {k: dict(v) for k, v in SPACE.items()}
    space["local"] = {m: v for m, v in SPACE["local"].items() if m != "microcloud20"}
    client = FakeClient(space=space)
    res = _read_member_resources(_service(client), NODE, "microcloud20")
    assert res.storage_partial is True
    assert res.storage_local_total == 0 and res.storage_shared_total == 2 * int(5.3 * TIB)


def test_parse_cpu_limit_and_counting():
    assert parse_cpu_limit(None) == 1 and parse_cpu_limit("") == 1
    assert parse_cpu_limit("4") == 4 and parse_cpu_limit(4) == 4
    assert parse_cpu_limit("0-3") == 4 and parse_cpu_limit("0,2,5") == 3
    assert parse_cpu_limit("garbage") == 1
    rows = [SimpleNamespace(location="m", status="Running", config={"limits.cpu": "2"}),
            SimpleNamespace(location="m", status="Stopped", config={}),
            SimpleNamespace(location="other", status="Running", config={})]
    assert count_member_instances(rows, "m") == (2, 1, 2)


class _CacheSpy:
    def __init__(self):
        self.sets = []

    def get(self, namespace, key):
        return None

    def set(self, namespace, key, value, ttl=None):
        self.sets.append((namespace, ttl, value))


def _app(client, monkeypatch, cache: _CacheSpy) -> TestClient:
    from app.services.lxd_service import get_lxd_service
    from app.core.rbac import get_current_user
    from app.core.org_scope import org_scope_dependency

    monkeypatch.setattr(cluster_members, "get_cache_manager", lambda: cache)
    app = FastAPI()
    app.include_router(cluster_members.router)
    app.dependency_overrides[get_lxd_service] = lambda: _service(client)
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def test_a_degraded_body_is_cached_briefly_and_a_complete_one_for_the_full_ttl(monkeypatch):
    cache = _CacheSpy()
    app = _app(FakeClient(instances_fail=True), monkeypatch, cache)
    body = app.get(f"/cluster-members/microcloud20/resources?node={NODE}").json()
    assert body["instances_partial"] is True
    assert [ttl for _, ttl, _ in cache.sets] == [DEGRADED_CACHE_TTL]

    cache = _CacheSpy()
    app = _app(FakeClient(), monkeypatch, cache)
    body = app.get(f"/cluster-members/microcloud20/resources?node={NODE}").json()
    assert body["instances_partial"] is False and body["storage_partial"] is False
    assert [ttl for _, ttl, _ in cache.sets] != [DEGRADED_CACHE_TTL]
    assert body["storage_shared_total"] == 2 * int(5.3 * TIB)
    assert body["cpu_used"] == 5


def test_the_route_reads_the_cluster_off_the_event_loop():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(cluster_members))
    handler = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "get_member_resources")
    to_thread_targets = [
        n.args[0].id
        for n in ast.walk(handler)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "to_thread"
        and n.args and isinstance(n.args[0], ast.Name)
    ]
    assert to_thread_targets == ["_read_member_resources"]
    api_reads = [n for n in ast.walk(handler) if isinstance(n, ast.Attribute) and n.attr == "api"]
    assert api_reads == [], "no pylxd read may run inline in the handler"
