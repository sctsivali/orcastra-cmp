"""
A key granted specific projects on a cluster stays inside them on the raw proxy, /proxy and /sync.

The project check only bound `request.project`. A key granted `projects: ["tenant-a"]` could still
change projects, storage pools, networks and profiles (none of which a project bounds), read every
project through `all-projects=true`, and list every project through /proxy list_projects and the
/sync payload. A `*` grant, which the integrator's connection keys carry, is untouched.

A double-encoded `project` query key also survived the raw proxy's one-decode pop: `%2570roject`
was decoded once to `%70roject`, re-emitted, and requests unescaped it to `project`, so LXD
answered for another project.
"""

from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import parse_qsl, urlsplit

import pytest
import requests
from fastapi import HTTPException

from app.core.proxy_paths import assert_project_scope_allows_path, resource_segments
from app.routers import integrations
from app.routers.integrations import (
    ProxyRequest,
    RawProxyRequest,
    SyncDataRequest,
    proxy_operation,
    raw_proxy,
    sync_data,
)
from app.services.lxd import health

CLUSTER_ID = "cluster-a"


def _key(projects: List[str]) -> Dict[str, Any]:
    return {
        "id": "oak_test",
        "app_name": "qa-narrow",
        "cluster_permissions": [
            {"cluster_id": CLUSTER_ID, "permissions": ["read", "write", "admin"], "projects": projects}
        ],
    }


NARROW = _key(["tenant-a"])
WILDCARD = _key(["*"])


class RecordingSession:
    """Records every URL, and answers the reads a limited write plan makes with plain resources."""

    def __init__(self) -> None:
        self.calls: List[str] = []
        self.writes: List[str] = []

    def _record(self, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append(url)
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"type": "sync", "metadata": []}'
        return response

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        response = self._record(url, **kwargs)
        if "/images/aliases/" in url:
            response._content = b'{"metadata": {"target": "fp1"}}'
        elif "/images/" in url or "/instances/" in url:
            response._content = b'{"metadata": {"profiles": ["default"], "config": {}, "devices": {}}}'
        return response

    def _write(self, url: str, **kwargs: Any) -> requests.Response:
        self.writes.append(url)
        return self._record(url, **kwargs)

    post = put = patch = delete = _write


class FakeClient:
    def __init__(self, session: Optional[RecordingSession] = None) -> None:
        self.api = SimpleNamespace(
            _api_endpoint="https://cluster-a.example.test:8443/1.0",
            _timeout=(3, 8),
            session=session or RecordingSession(),
        )
        self.projects = SimpleNamespace(
            all=lambda: [
                SimpleNamespace(name=name, description=f"{name} project")
                for name in ("default", "tenant-a", "tenant-b")
            ]
        )
        self.instances = SimpleNamespace(
            all=lambda: [SimpleNamespace(name="web-1", status="Running", type="container")]
        )


class FakeManager:
    def __init__(self, client: FakeClient) -> None:
        self.client = client

    def get_client(self, cluster_id: str) -> FakeClient:
        return self.client

    def get_project_client(self, cluster_id: str, project: str, timeout: Any = None) -> FakeClient:
        return self.client


class FakeService:
    def __init__(self, client: FakeClient) -> None:
        self.connection_manager = FakeManager(client)


@pytest.fixture(autouse=True)
def _reset_breaker() -> Iterator[None]:
    health.clear(CLUSTER_ID)
    yield
    health.clear(CLUSTER_ID)


async def _raw(
    api_key: Dict[str, Any], method: str, path: str, project: Optional[str] = None,
    body: Optional[Dict[str, Any]] = None,
) -> tuple:
    session = RecordingSession()
    try:
        response = await raw_proxy(
            request=RawProxyRequest(
                cluster_id=CLUSTER_ID,
                method=method,
                path=path,
                project=project,
                body=body if body is not None else ({} if method in ("POST", "PUT", "PATCH") else None),
            ),
            api_key=api_key,
            lxd_service=FakeService(FakeClient(session)),
            _=None,
        )
    except HTTPException as exc:
        return exc, session
    return response, session


UNBOUNDABLE_WRITES = [
    ("PUT", "/1.0/projects/tenant-b"),
    ("DELETE", "/1.0/projects/tenant-b"),
    ("POST", "/1.0/projects"),
    ("PATCH", "/1.0/profiles/default"),
    ("POST", "/1.0/networks"),
    ("DELETE", "/1.0/networks/lxdbr0"),
    ("POST", "/1.0/storage-pools"),
    ("DELETE", "/1.0/Storage-Pools/default/"),
    ("PUT", "/1.0/%70rofiles/default"),
    ("PUT", "/1.0"),
    ("PATCH", "/1.0/"),
    ("PUT", "/1.0/cluster"),
    ("PUT", "/1.0/cluster/certificate"),
    ("POST", "/1.0/cluster/members"),
    ("POST", "/1.0/cluster/members/m1/state"),
    ("PUT", "/1.0/warnings/0f3c9a"),
    ("POST", "/1.0/network-acls"),
    ("POST", "/1.0/storage-pools/default/volumes/custom"),
    ("PUT", "/1.0projects/tenant-b"),
    ("POST", "/1.0/instances-archive"),
]

# What a project grant does bound. A narrow key keeps every one of these.
BOUNDED_WRITES = [
    ("POST", "/1.0/instances"),
    ("PATCH", "/1.0/instances/web-1"),
    ("PUT", "/1.0/instances/web-1/state"),
    ("DELETE", "/1.0/instances/web-1"),
    ("POST", "/1.0/instances/web-1/snapshots"),
    ("DELETE", "/1.0/instances/web-1/snapshots/snap0"),
    ("POST", "/1.0/images"),
    ("POST", "/1.0/images/aliases"),
    ("DELETE", "/1.0/images/0f3c9a1b2c3d"),
    ("DELETE", "/1.0/operations/5b1e0a4c-0000-4000-8000-000000000001"),
]

ALL_PROJECTS = [
    "/1.0/instances?all-projects=true",
    "/1.0/instances?recursion=1&all-projects=1",
    "/1.0/instances?ALL-PROJECTS=true",
    "/1.0/instances?%61ll-projects=true",
    "/1.0/instances?%2561ll-projects=true",
    "/1.0/storage-pools/default/volumes?all-projects=true",
]


@pytest.mark.parametrize("method,path", UNBOUNDABLE_WRITES)
async def test_a_narrow_key_cannot_change_what_no_project_bounds(method: str, path: str) -> None:
    outcome, session = await _raw(NARROW, method, path, project="tenant-a")
    assert isinstance(outcome, HTTPException)
    assert outcome.status_code == 403
    assert session.calls == []


@pytest.mark.parametrize("path", ALL_PROJECTS)
async def test_a_narrow_key_cannot_read_all_projects(path: str) -> None:
    outcome, session = await _raw(NARROW, "GET", path, project="tenant-a")
    assert isinstance(outcome, HTTPException)
    assert outcome.status_code == 403
    assert session.calls == []


def _is_server_state(path: str) -> bool:
    """The bare /1.0 or /cluster: refused for every grant by assert_server_state_unchanged."""
    segments = resource_segments(path)
    return not segments or segments[0] == "cluster"


@pytest.mark.parametrize(
    "method,path", [(m, p) for m, p in UNBOUNDABLE_WRITES if not _is_server_state(p)]
)
async def test_a_wildcard_key_is_untouched_by_the_write_rule(method: str, path: str) -> None:
    outcome, session = await _raw(WILDCARD, method, path)
    assert outcome.status_code == 200
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "method,path", [(m, p) for m, p in UNBOUNDABLE_WRITES if _is_server_state(p)]
)
async def test_a_wildcard_key_still_cannot_change_the_server_or_cluster(method: str, path: str) -> None:
    outcome, session = await _raw(WILDCARD, method, path)
    assert isinstance(outcome, HTTPException)
    assert outcome.status_code == 403
    assert session.calls == []


@pytest.mark.parametrize("path", ALL_PROJECTS)
async def test_a_wildcard_key_still_reads_all_projects(path: str) -> None:
    outcome, session = await _raw(WILDCARD, "GET", path)
    assert outcome.status_code == 200
    assert len(session.calls) == 1


@pytest.mark.parametrize("method,path", BOUNDED_WRITES)
async def test_a_narrow_key_may_change_what_its_project_bounds(method: str, path: str) -> None:
    outcome, session = await _raw(NARROW, method, path, project="tenant-a")
    assert outcome.status_code == 200
    # note: a create first reads the profile it applies, see the applied-profile tests
    writes = [url for url in session.calls if "/profiles/" not in url]
    assert writes == [f"https://cluster-a.example.test:8443{path}?project=tenant-a"]


async def test_a_narrow_key_still_works_inside_its_project() -> None:
    outcome, session = await _raw(NARROW, "POST", "/1.0/instances", project="tenant-a")
    assert outcome.status_code == 200
    assert session.calls == ["https://cluster-a.example.test:8443/1.0/profiles/default",
                             "https://cluster-a.example.test:8443/1.0/instances?project=tenant-a"]


async def test_a_narrow_key_may_still_read_topology() -> None:
    outcome, session = await _raw(NARROW, "GET", "/1.0/networks", project="tenant-a")
    assert outcome.status_code == 200
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "path",
    [
        "/1.0/instances?%2570roject=tenant-b",
        "/1.0/instances?%70roject=tenant-b",
        "/1.0/instances?%252570roject=tenant-b",
        "/1.0/instances?Project=tenant-b",
        "/1.0/instances?project=tenant-b",
    ],
)
async def test_a_smuggled_project_key_never_reaches_lxd(path: str) -> None:
    outcome, session = await _raw(NARROW, "GET", path, project="tenant-a")
    assert outcome.status_code == 200
    sent = requests.Request("GET", session.calls[0]).prepare().url
    assert sent == "https://cluster-a.example.test:8443/1.0/instances?project=tenant-a"


def _query_lxd_reads(url: str) -> List[Tuple[str, str]]:
    """The query LXD parses from what requests puts on the wire: no fragment, decoded once."""
    sent = requests.Request("GET", url).prepare().url
    return parse_qsl(urlsplit(sent).query, keep_blank_values=True)


# A name or fragment that changes the query once raw_proxy has decoded it. The guard decided on
# the raw split, where `x%26all-projects` is one longer name, while the old join wrote it back as
# `x&all-projects`, and a `#` turned the enforced `&project=tenant-a` into a fragment.
SMUGGLED_QUERIES = [
    "/1.0/instances?x%26all-projects=true",
    "/1.0/instances?x%26project=tenant-b",
    "/1.0/instances?x%3Dy%26project=tenant-b",
    "/1.0/instances?a%23=1",
    "/1.0/instances?a%3F%26project=tenant-b",
    "/1.0/instances?x=1%26project%3Dtenant-b",
    "/1.0/instances#x",
    "/1.0/instances?recursion=1#x",
    "/1.0/instances?recursion=1#&project=tenant-b",
]


@pytest.mark.parametrize("path", SMUGGLED_QUERIES)
async def test_a_smuggled_name_never_changes_the_query_lxd_reads(path: str) -> None:
    outcome, session = await _raw(NARROW, "GET", path, project="tenant-a")
    assert outcome.status_code == 200
    pairs = _query_lxd_reads(session.calls[0])
    assert "all-projects" not in [name for name, _ in pairs], session.calls[0]
    assert [value for name, value in pairs if name == "project"] == ["tenant-a"], session.calls[0]


@pytest.mark.parametrize(
    "method,path",
    [("PUT", "/1.0projects/tenant-b"), ("PATCH", "/1.0profiles/default"), ("POST", "/1.0networks")],
)
async def test_a_glued_version_never_reaches_an_unbounded_resource(method: str, path: str) -> None:
    outcome, session = await _raw(NARROW, method, path, project="tenant-a")
    for url in session.calls:
        resource = urlsplit(requests.Request(method, url).prepare().url).path.split("/")[2]
        assert resource not in ("projects", "profiles", "networks", "storage-pools"), url


def test_the_guard_leaves_a_missing_grant_closed() -> None:
    with pytest.raises(HTTPException):
        assert_project_scope_allows_path("/1.0/profiles/default", "PATCH", None)
    with pytest.raises(HTTPException):
        assert_project_scope_allows_path("/1.0/instances?all-projects=true", "GET", [])


async def _proxy_list_projects(api_key: Dict[str, Any], project: str) -> Any:
    return await proxy_operation(
        request=ProxyRequest(cluster_id=CLUSTER_ID, operation="list_projects", project=project),
        api_key=api_key,
        lxd_service=FakeService(FakeClient()),
        _=None,
    )


async def test_proxy_list_projects_is_filtered_for_a_narrow_key() -> None:
    response = await _proxy_list_projects(NARROW, "tenant-a")
    assert response.success is True
    assert [p["name"] for p in response.data] == ["tenant-a"]


async def test_proxy_list_projects_is_whole_for_a_wildcard_key() -> None:
    response = await _proxy_list_projects(WILDCARD, "default")
    assert [p["name"] for p in response.data] == ["default", "tenant-a", "tenant-b"]


async def _sync(api_key: Dict[str, Any]) -> Any:
    return await sync_data(
        request=SyncDataRequest(include_clusters=False, include_projects=True, include_instances=True),
        api_key=api_key,
        lxd_service=FakeService(FakeClient()),
        db=None,
    )


async def test_sync_projects_are_filtered_for_a_narrow_key() -> None:
    result = await _sync(NARROW)
    assert [p["name"] for p in result.projects] == ["tenant-a"]
    assert result.instances == [], "the default project's instances are outside this grant"


async def test_sync_is_whole_for_a_wildcard_key() -> None:
    result = await _sync(WILDCARD)
    assert [p["name"] for p in result.projects] == ["default", "tenant-a", "tenant-b"]
    assert [i["name"] for i in result.instances] == ["web-1"]


async def test_sync_instances_follow_a_grant_that_covers_default() -> None:
    result = await _sync(_key(["default", "tenant-a"]))
    assert [p["name"] for p in result.projects] == ["default", "tenant-a"]
    assert [i["name"] for i in result.instances] == ["web-1"]


# -- the body cannot take a project-limited key out of its project -----------------------------

ROOT_DISK = {"root": {"type": "disk", "path": "/", "pool": "default"}}
FOREIGN_BODIES = [
    ("POST", "/1.0/instances", {"name": "c1", "source": {"type": "copy", "source": "db", "project": "tenant-b"}}),
    ("POST", "/1.0/instances/web", {"name": "web", "project": "tenant-b"}),
    ("POST", "/1.0/images", {"source": {"type": "instance", "name": "db", "project": "tenant-b"}}),
    ("POST", "/1.0/instances", {"name": "c1", "target": {"project": "tenant-b"}}),
]
HOST_BODIES = [
    ("POST", "/1.0/instances", {"name": "c1", "config": {"security.privileged": "true"}}),
    ("PUT", "/1.0/instances/web", {"config": {"raw.lxc": "lxc.apparmor.profile=unconfined"}}),
    ("PATCH", "/1.0/instances/web", {"config": {"Security.IDMap.Size": "65536"}}),
    ("PATCH", "/1.0/instances/web", {"config": {"linux.kernel_modules": "overlay"}}),
    ("PATCH", "/1.0/instances/web", {"devices": {"host": {"type": "disk", "source": "/", "path": "/mnt"}}}),
    ("PATCH", "/1.0/instances/web", {"devices": {"sock": {"type": "proxy", "listen": "tcp:0.0.0.0:1",
                                                            "connect": "unix:/var/lib/lxd/unix.socket"}}}),
    ("PATCH", "/1.0/instances/web", {"devices": {"r": {"type": "disk", "source": "ceph:rbd/vol", "path": "/r"}}}),
    ("PATCH", "/1.0/instances/web", {"devices": {"eth1": {"type": "nic", "nictype": "physical", "parent": "eno1"}}}),
    ("POST", "/1.0/instances", {"name": "c1", "devices": {"d": {"type": "unix-block", "source": "/dev/sda"}}}),
]
PLAIN_BODIES = [
    ("POST", "/1.0/instances", {"name": "c1", "source": {"type": "image", "alias": "ubuntu"},
                                "devices": dict(ROOT_DISK), "config": {"limits.cpu": "2"}}),
    ("POST", "/1.0/instances", {"name": "c1", "source": {"type": "copy", "source": "web", "project": "tenant-a"}}),
    ("PATCH", "/1.0/instances/web", {"devices": {"data": {"type": "disk", "pool": "default", "source": "vol1",
                                                            "path": "/data"}}}),
    ("PATCH", "/1.0/instances/web", {"devices": {"eth0": {"type": "nic", "network": "lxdbr0"}}}),
    ("PUT", "/1.0/instances/web/state", {"action": "stop", "force": True}),
    # a GPU, a switch set off and a VM's own config drive do not reach the host
    ("PATCH", "/1.0/instances/web", {"devices": {"g": {"type": "gpu"}}}),
    ("PATCH", "/1.0/instances/web", {"config": {"security.privileged": "false", "security.idmap.isolated": "true"}}),
    ("PATCH", "/1.0/instances/web", {"devices": {"cloud": {"type": "disk", "source": "cloud-init:config"}}}),
]


@pytest.mark.parametrize("method,path,body", FOREIGN_BODIES + HOST_BODIES)
async def test_a_narrow_key_body_cannot_leave_its_project(method: str, path: str, body: Dict[str, Any]) -> None:
    outcome, session = await _raw(NARROW, method, path, project="tenant-a", body=body)
    assert isinstance(outcome, HTTPException)
    assert outcome.status_code == 403
    assert session.calls == []


@pytest.mark.parametrize("method,path,body", PLAIN_BODIES)
async def test_a_narrow_key_body_inside_its_project_goes_through(method: str, path: str, body: Dict[str, Any]) -> None:
    outcome, session = await _raw(NARROW, method, path, project="tenant-a", body=body)
    assert outcome.status_code == 200
    # note: a write first reads what it applies (profiles, a copy's source, an image), see the applied-profile tests
    assert len(session.writes) == 1


@pytest.mark.parametrize("method,path,body", FOREIGN_BODIES + HOST_BODIES)
async def test_a_wildcard_key_body_is_untouched(method: str, path: str, body: Dict[str, Any]) -> None:
    outcome, session = await _raw(WILDCARD, method, path, body=body)
    assert outcome.status_code == 200
    assert len(session.calls) == 1



@pytest.mark.parametrize(
    "params,reason",
    [
        ({"name": "vm", "source": {"type": "none"}, "config": {"security.privileged": "true"}}, "security.privileged"),
        ({"name": "vm", "source": {"type": "copy", "source": "x", "project": "tenant-b"}}, "tenant-b"),
        ({"name": "vm", "source": {"type": "none"}, "devices": {"h": {"type": "disk", "source": "/", "path": "/h"}}}, "host path"),
    ],
)
async def test_proxy_create_holds_a_narrow_key_to_the_body_rule(monkeypatch, params: Dict[str, Any], reason: str) -> None:
    """/proxy create_instance sends params as the instance body, so it gets the raw proxy's body rule."""
    sent: List[Any] = []
    monkeypatch.setattr(integrations, "_execute_proxy_operation", lambda *a, **_k: sent.append(a) or {"ok": True})
    with pytest.raises(HTTPException) as caught:
        await proxy_operation(
            request=ProxyRequest(cluster_id=CLUSTER_ID, operation="create_instance", project="tenant-a", params=params),
            api_key=NARROW, lxd_service=FakeService(FakeClient()), _=None,
        )
    assert caught.value.status_code == 403 and reason in caught.value.detail
    assert sent == [], "the refused body reached the cluster"


async def test_proxy_create_still_works_inside_the_grant_and_for_a_star_key(monkeypatch) -> None:
    sent: List[Any] = []
    monkeypatch.setattr(integrations, "_execute_proxy_operation", lambda *a, **_k: sent.append(a[2]) or {"ok": True})
    plain = {"name": "vm", "source": {"type": "none"}}
    answer = await proxy_operation(
        request=ProxyRequest(cluster_id=CLUSTER_ID, operation="create_instance", project="tenant-a", params=plain),
        api_key=NARROW, lxd_service=FakeService(FakeClient()), _=None,
    )
    assert answer.success is True
    privileged = dict(plain, config={"security.privileged": "true"})
    answer = await proxy_operation(
        request=ProxyRequest(cluster_id=CLUSTER_ID, operation="create_instance", project="tenant-a", params=privileged),
        api_key=_key(["*"]), lxd_service=FakeService(FakeClient()), _=None,
    )
    assert answer.success is True and sent == ["create_instance", "create_instance"]
