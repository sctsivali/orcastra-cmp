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

from app.core.proxy_paths import assert_project_scope_allows_path
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
    def __init__(self) -> None:
        self.calls: List[str] = []

    def _record(self, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append(url)
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"type": "sync", "metadata": []}'
        return response

    get = post = put = patch = delete = _record


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
    api_key: Dict[str, Any], method: str, path: str, project: Optional[str] = None
) -> tuple:
    session = RecordingSession()
    try:
        response = await raw_proxy(
            request=RawProxyRequest(
                cluster_id=CLUSTER_ID,
                method=method,
                path=path,
                project=project,
                body={} if method in ("POST", "PUT", "PATCH") else None,
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


@pytest.mark.parametrize("method,path", UNBOUNDABLE_WRITES)
async def test_a_wildcard_key_is_untouched_by_the_write_rule(method: str, path: str) -> None:
    outcome, session = await _raw(WILDCARD, method, path)
    assert outcome.status_code == 200
    assert len(session.calls) == 1


@pytest.mark.parametrize("path", ALL_PROJECTS)
async def test_a_wildcard_key_still_reads_all_projects(path: str) -> None:
    outcome, session = await _raw(WILDCARD, "GET", path)
    assert outcome.status_code == 200
    assert len(session.calls) == 1


async def test_a_narrow_key_still_works_inside_its_project() -> None:
    outcome, session = await _raw(NARROW, "POST", "/1.0/instances", project="tenant-a")
    assert outcome.status_code == 200
    assert session.calls == ["https://cluster-a.example.test:8443/1.0/instances?project=tenant-a"]


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
