"""
A key granted specific projects on a cluster stays inside them on the raw proxy, /proxy and /sync.

The raw proxy bound only `request.project`. A key granted `projects: ["tenant-a"]` could still read
every project with `all-projects=true` in any spelling, and a double-encoded `project` query key
survived the one-decode pop: `%2570roject` was decoded once to `%70roject`, re-emitted, and
requests unescaped it to `project`, so LXD answered for another project. A decoded `&` or `#` in a
query name, joined back raw, added a key LXD read or turned the authorised `&project=` into a
fragment requests never sends. A `*` grant, which the integrator's connection keys carry, is
untouched.

Ported from the RC4-hotfix2 line and adapted to this line: /proxy list_projects and /sync were
already narrowed here (`_within_granted_projects`), and /sync reads instances from every project,
so the tests assert the effect on this line's shapes.
"""

from types import SimpleNamespace
from typing import Any, Dict, List, Tuple
from urllib.parse import urlsplit

import pytest
import requests
from fastapi import HTTPException

from app.core.api_key_capabilities import assert_project_scope_allows_path
from app.routers import integrations
from app.routers.integrations import ProxyRequest, SyncDataRequest, proxy_operation, sync_data
from tests._raw_proxy_fakes import (  # noqa: F401 - fixtures are registered by import
    CLUSTER_ID,
    ENDPOINT,
    NARROW,
    WILDCARD,
    _clean_breaker,
    call_raw_proxy,
    key,
    lxd_reads,
)

ALL_PROJECTS = [
    "/1.0/instances?all-projects=true",
    "/1.0/instances?recursion=1&all-projects=1",
    "/1.0/instances?ALL-PROJECTS=true",
    "/1.0/instances?%61ll-projects=true",
    "/1.0/instances?%2561ll-projects=true",
    "/1.0/instances?+all-projects=true",
    "/1.0/instances?all-projects=true#x",
    "/1.0/storage-pools/default/volumes?all-projects=true",
    "/1.0/operations?all-projects=true&recursion=1",
]


@pytest.mark.parametrize("method", ["GET", "DELETE"])
@pytest.mark.parametrize("path", ALL_PROJECTS)
async def test_a_narrow_key_cannot_read_all_projects(path: str, method: str) -> None:
    outcome, session = await call_raw_proxy(NARROW, method, path, project="tenant-a")
    assert isinstance(outcome, HTTPException)
    assert outcome.status_code == 403
    assert "all-projects" in outcome.detail
    assert session.calls == []


@pytest.mark.parametrize("path", ALL_PROJECTS)
async def test_a_wildcard_key_still_reads_all_projects(path: str) -> None:
    outcome, session = await call_raw_proxy(WILDCARD, "GET", path)
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
        "/1.0/instances?project=tenant-b&project=tenant-c",
    ],
)
async def test_a_smuggled_project_key_never_reaches_lxd(path: str) -> None:
    outcome, session = await call_raw_proxy(NARROW, "GET", path, project="tenant-a")
    assert outcome.status_code == 200
    sent = requests.Request("GET", session.urls[0]).prepare().url
    assert sent == f"{ENDPOINT}/instances?project=tenant-a"


# A name or fragment that changes the query once raw_proxy has decoded it. `x%26all-projects` is
# one longer name, while the old join wrote it back as `x&all-projects`, and a `#` turned the
# authorised `&project=tenant-a` into a fragment.
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
    outcome, session = await call_raw_proxy(NARROW, "GET", path, project="tenant-a")
    assert outcome.status_code == 200
    _, pairs = lxd_reads(session.urls[0])
    assert "all-projects" not in [name for name, _ in pairs], session.urls[0]
    assert [value for name, value in pairs if name == "project"] == ["tenant-a"], session.urls[0]


@pytest.mark.parametrize(
    "method,path",
    [("PUT", "/1.0projects/tenant-b"), ("PATCH", "/1.0profiles/default"), ("POST", "/1.0networks")],
)
async def test_a_glued_version_never_reaches_an_unbounded_resource(method: str, path: str) -> None:
    _, session = await call_raw_proxy(NARROW, method, path, project="tenant-a")
    for url in session.urls:
        resource = urlsplit(requests.Request(method, url).prepare().url).path.split("/")[2]
        assert resource not in ("projects", "profiles", "networks", "storage-pools"), url


# This line's four-prefix rule, read through the canonical path: a fragment, a dot or an encoded
# spelling no longer steps around it.
@pytest.mark.parametrize(
    "method,path",
    [
        ("PUT", "/1.0/projects/tenant-b"),
        ("DELETE", "/1.0/projects/tenant-b#x"),
        ("PATCH", "/1.0/%70rofiles/default"),
        ("PATCH", "/1.0/%2570rofiles/default"),
        ("POST", "/1.0/networks#"),
        ("DELETE", "/1.0/Storage-Pools/default/"),
    ],
)
async def test_a_narrow_key_cannot_change_an_unbounded_resource_in_any_spelling(method: str, path: str) -> None:
    outcome, session = await call_raw_proxy(NARROW, method, path, project="tenant-a")
    assert isinstance(outcome, HTTPException)
    assert outcome.status_code == 403
    assert session.calls == []


def test_the_guard_leaves_a_missing_grant_closed() -> None:
    with pytest.raises(HTTPException):
        assert_project_scope_allows_path("/1.0/profiles/default", "PATCH", None)
    with pytest.raises(HTTPException):
        assert_project_scope_allows_path("/1.0/instances?all-projects=true", "GET", [])


# -- /proxy list_projects and /sync ------------------------------------------------------------

PROJECTS = ("default", "tenant-a", "tenant-b")


async def _proxy_list_projects(api_key: Dict[str, Any], project: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    executed: List[Tuple[str, str]] = []

    def fake_execute(lxd_service: Any, cluster_id: str, operation: str, params: Dict[str, Any],
                     project_name: str, wait: bool = True) -> List[Dict[str, str]]:
        executed.append((operation, project_name))
        return [{"name": name, "description": f"{name} project"} for name in PROJECTS]

    monkeypatch.setattr(integrations, "execute_proxy_operation", fake_execute)
    response = await proxy_operation(
        request=ProxyRequest(cluster_id=CLUSTER_ID, operation="list_projects", project=project),
        api_key=api_key,
        lxd_service=SimpleNamespace(),
        _=None,
    )
    assert executed == [("list_projects", project)]
    return response


async def test_proxy_list_projects_is_filtered_for_a_narrow_key(monkeypatch: pytest.MonkeyPatch) -> None:
    response = await _proxy_list_projects(NARROW, "tenant-a", monkeypatch)
    assert response.success is True
    assert [p["name"] for p in response.data] == ["tenant-a"]


async def test_proxy_list_projects_is_whole_for_a_wildcard_key(monkeypatch: pytest.MonkeyPatch) -> None:
    response = await _proxy_list_projects(WILDCARD, "default", monkeypatch)
    assert [p["name"] for p in response.data] == list(PROJECTS)


def _sync_service() -> SimpleNamespace:
    client = SimpleNamespace(
        projects=SimpleNamespace(
            all=lambda: [SimpleNamespace(name=name, description=f"{name} project") for name in PROJECTS]
        )
    )
    vms = [
        SimpleNamespace(name=f"web-{project}", project=project, status_code=103, status="Running",
                        type="container", architecture="x86_64", config={}, expanded_devices={},
                        ip_addresses=[])
        for project in PROJECTS
    ]
    return SimpleNamespace(
        connection_manager=SimpleNamespace(get_client=lambda cluster_id: client),
        instance_manager=SimpleNamespace(fetch_node_instances=lambda cluster_id: (vms, True)),
    )


async def _sync(api_key: Dict[str, Any]) -> Any:
    return await sync_data(
        request=SyncDataRequest(include_clusters=False, include_projects=True, include_instances=True),
        api_key=api_key,
        lxd_service=_sync_service(),
        db=None,
    )


async def test_sync_projects_and_instances_are_filtered_for_a_narrow_key() -> None:
    result = await _sync(NARROW)
    assert [p["name"] for p in result.projects] == ["tenant-a"]
    assert [i["name"] for i in result.instances] == ["web-tenant-a"]


async def test_sync_is_whole_for_a_wildcard_key() -> None:
    result = await _sync(WILDCARD)
    assert [p["name"] for p in result.projects] == list(PROJECTS)
    assert [i["name"] for i in result.instances] == [f"web-{project}" for project in PROJECTS]


async def test_sync_follows_a_grant_that_covers_default() -> None:
    result = await _sync(key(["default", "tenant-a"]))
    assert [p["name"] for p in result.projects] == ["default", "tenant-a"]
    assert [i["name"] for i in result.instances] == ["web-default", "web-tenant-a"]
