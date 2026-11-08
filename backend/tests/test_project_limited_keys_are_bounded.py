"""
A key granted specific projects on a cluster stays inside them on the raw proxy, /proxy and /sync.

The raw proxy bound only `request.project`. A key granted `projects: ["tenant-a"]` could still read
every project with `all-projects=true` in any spelling, and a double-encoded `project` query key
survived the one-decode pop: `%2570roject` was decoded once to `%70roject`, re-emitted, and
requests unescaped it to `project`, so LXD answered for another project. A decoded `&` or `#` in a
query name, joined back raw, added a key LXD read or turned the authorised `&project=` into a
fragment requests never sends. A `*` grant, which the integrator's connection keys carry, is
untouched.

A narrow key could also change anything outside the four prefixes this line refused (the server
config, the cluster, network ACLs and zones, warnings, and every resource LXD adds above
projects). The write rule is now an allowlist: instances, images and operations.

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
from app.core.limited_key_writes import host_reach
from app.core.raw_proxy_path import canonical_proxy_path
from app.routers import integrations
from app.routers.integrations import ProxyRequest, SyncDataRequest, proxy_operation, sync_data
from tests._raw_proxy_fakes import (  # noqa: F401 - fixtures are registered by import
    CLUSTER_ID,
    ENDPOINT,
    NARROW,
    WILDCARD,
    RecordingSession,
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


# A project grant is a boundary for instances (with their snapshots), images and operations only.
# Everything else a narrow key could write sits above a project or is shared with `default`.
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
    ("DELETE", "/1.0/warnings/0f3c9a"),
    ("POST", "/1.0/network-acls"),
    ("POST", "/1.0/network-zones"),
    ("PUT", "/1.0/network-address-sets/set-a"),
    ("POST", "/1.0/storage-pools/default/volumes/custom"),
    ("POST", "/1.0/storage-pools/default/buckets"),
    ("PUT", "/1.0projects/tenant-b"),
    ("POST", "/1.0/instances-archive"),
    ("DELETE", "/1.0/storage-pools-not-really"),
    ("POST", "/1.0/placement-groups"),
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
    ("PUT", "/1.0/INSTANCES/web-1/state"),
]


def _is_server_state(path: str) -> bool:
    """The bare /1.0 or /cluster: refused for every grant by assert_server_state_unchanged."""
    segments = canonical_proxy_path(path).lowered
    return not segments or segments[0] == "cluster"


@pytest.mark.parametrize("method,path", UNBOUNDABLE_WRITES)
async def test_a_narrow_key_cannot_change_what_no_project_bounds(method: str, path: str) -> None:
    outcome, session = await call_raw_proxy(NARROW, method, path, project="tenant-a")
    assert isinstance(outcome, HTTPException)
    assert outcome.status_code == 403
    assert session.calls == []


@pytest.mark.parametrize("method,path", [(m, p) for m, p in UNBOUNDABLE_WRITES if not _is_server_state(p)])
async def test_a_wildcard_key_is_untouched_by_the_write_rule(method: str, path: str) -> None:
    outcome, session = await call_raw_proxy(WILDCARD, method, path)
    assert outcome.status_code == 200
    assert len(session.calls) == 1


@pytest.mark.parametrize("method,path", [(m, p) for m, p in UNBOUNDABLE_WRITES if _is_server_state(p)])
async def test_a_wildcard_key_still_cannot_change_the_server_or_cluster(method: str, path: str) -> None:
    outcome, session = await call_raw_proxy(WILDCARD, method, path)
    assert isinstance(outcome, HTTPException)
    assert outcome.status_code == 403
    assert session.calls == []


class _OwnOperationCluster(RecordingSession):
    """Answers the ownership read raw_proxy makes before a narrow key reaches one operation."""

    def _answer(self, verb: str, url: str, **kwargs: Any) -> requests.Response:
        if verb == "GET" and "/operations/" in url:
            self.calls.append((verb, url, kwargs))
            response = requests.Response()
            response.status_code = 200
            response._content = (
                b'{"type": "sync", "metadata": {"resources": '
                b'{"instances": ["/1.0/instances/web?project=tenant-a"]}}}'
            )
            return response
        return super()._answer(verb, url, **kwargs)


@pytest.mark.parametrize("method,path", BOUNDED_WRITES)
async def test_a_narrow_key_may_change_what_its_project_bounds(method: str, path: str) -> None:
    # note: an operation id is cluster-wide, so for /operations/<id> the bound is the operation's
    # owner, read first (test_a_raw_operation_read_is_bounded_by_its_owner.py), not ?project=.
    outcome, session = await call_raw_proxy(
        NARROW, method, path, project="tenant-a", session=_OwnOperationCluster()
    )
    assert outcome.status_code == 200
    # note: a create first reads the profile it applies, see test_an_applied_profile_cannot_reach_the_host
    writes = [url for verb, url, _ in session.calls if verb == method]
    assert len(writes) == 1
    segments, query = lxd_reads(writes[0], method)
    assert segments == list(canonical_proxy_path(path).segments)
    assert query == [("project", "tenant-a")]


async def test_a_narrow_key_may_still_read_topology() -> None:
    outcome, session = await call_raw_proxy(NARROW, "GET", "/1.0/networks", project="tenant-a")
    assert outcome.status_code == 200
    assert len(session.calls) == 1


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
                     project_name: str, wait: bool = True, **_: Any) -> List[Dict[str, str]]:
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


# -- the body cannot take a project-limited key out of its project (a335b0d, 441213a) ---------------


class _PlainCluster(RecordingSession):
    """Answers the reads a limited write plan makes with plain resources: every profile, instance and
    image exists and reaches nothing, and every alias resolves."""

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        response = super().get(url, **kwargs)
        if "/images/aliases/" in url:
            response._content = b'{"metadata": {"target": "fp1"}}'
        elif any(part in url for part in ("/images/", "/instances/", "/profiles/")):
            response._content = b'{"metadata": {"profiles": ["default"], "config": {}, "devices": {}}}'
        return response

    @property
    def writes(self) -> List[str]:
        return [url for verb, url, _ in self.calls if verb != "GET"]


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
    outcome, session = await call_raw_proxy(NARROW, method, path, project="tenant-a", body=body)
    assert isinstance(outcome, HTTPException)
    assert outcome.status_code == 403
    assert session.calls == []


@pytest.mark.parametrize("method,path,body", PLAIN_BODIES)
async def test_a_narrow_key_body_inside_its_project_goes_through(method: str, path: str, body: Dict[str, Any]) -> None:
    outcome, session = await call_raw_proxy(NARROW, method, path, project="tenant-a", body=body,
                                            session=_PlainCluster())
    assert outcome.status_code == 200
    assert len(session.writes) == 1


# A body `target` object names a host the daemon sends to, which no key may (core/key_pull_sources.py).
_TARGETED = [case for case in FOREIGN_BODIES + HOST_BODIES if isinstance(case[2].get("target"), dict)]


@pytest.mark.parametrize("method,path,body", [c for c in FOREIGN_BODIES + HOST_BODIES if c not in _TARGETED])
async def test_a_wildcard_key_body_is_untouched(method: str, path: str, body: Dict[str, Any]) -> None:
    outcome, session = await call_raw_proxy(WILDCARD, method, path, body=body)
    assert outcome.status_code == 200
    assert len(session.calls) == 1


@pytest.mark.parametrize("method,path,body", _TARGETED)
async def test_a_wildcard_key_naming_a_target_is_refused_like_any_key(method: str, path: str, body: Dict[str, Any]) -> None:
    outcome, session = await call_raw_proxy(WILDCARD, method, path, body=body)
    assert outcome.status_code == 403 and outcome.detail["error"] == "pull_source_not_allowed"
    assert session.calls == []


# The value table the rule is judged by (441213a): a switch is refused only when it is not off,
# raw.*, kernel modules and an idmap base or size whenever set, a disk from outside a storage pool
# for any source but LXD's own VM drives, and a GPU stays allowed.
REACHING = [
    ({"config": {"security.privileged": "true"}}, "security.privileged"),
    ({"config": {"Security.Privileged": " YES "}}, "Security.Privileged"),
    ({"config": {"security.privileged": "garbage"}}, "security.privileged"),
    ({"config": {"raw.lxc": "lxc.apparmor.profile=unconfined"}}, "raw.lxc"),
    ({"config": {"security.idmap.base": "0"}}, "security.idmap.base"),
    ({"config": {"security.idmap.size": "65536"}}, "security.idmap.size"),
    ({"config": {"security.syscalls.intercept.mount.allowed": "ext4"}}, "intercept"),
    ({"config": {"linux.kernel_modules": "overlay"}}, "kernel_modules"),
    ({"devices": {"h": {"type": "disk", "source": "/", "path": "/mnt/h"}}}, "host path"),
    ({"devices": {"r": {"type": "disk", "source": "ceph:rbd-pool/other-project_vol", "path": "/r"}}}, "storage pool"),
    ({"devices": {"f": {"type": "disk", "source": "cephfs:fs/some/path", "path": "/f"}}}, "storage pool"),
    ({"devices": {"s": {"type": "proxy", "listen": "tcp:0.0.0.0:1", "connect": "unix:/var/lib/x"}}}, "proxy"),
    ({"devices": {"u": {"type": "usb"}}}, "usb"),
    ({"devices": {"k": {"type": "unix-char", "source": "/dev/kvm"}}}, "unix-char"),
    ({"devices": {"e": {"type": "nic", "nictype": "physical", "parent": "eno1"}}}, "physical"),
]
SAFE = [
    {"config": {"security.privileged": "false"}},
    {"config": {"security.privileged": "0", "security.syscalls.intercept.mknod": "false"}},
    {"config": {"security.idmap.isolated": "true"}},
    {"config": {"security.idmap.isolated": "false", "raw.lxc": ""}},
    {"config": {"limits.cpu": "2", "security.nesting": "true"}},
    {"devices": {"g": {"type": "gpu", "gputype": "physical"}}},
    {"devices": {"data": {"type": "disk", "pool": "default", "source": "vol1", "path": "/data"}}},
    {"devices": {"cloud": {"type": "disk", "source": "cloud-init:config"}}},
    {"devices": {"agent": {"type": "disk", "source": "agent:config"}}},
    {"devices": {"eth0": {"type": "nic", "network": "br0"}, "root": {"type": "disk", "path": "/", "pool": "default"}}},
]


@pytest.mark.parametrize("body,reason", REACHING)
def test_host_reach_is_judged_by_value(body: Dict[str, Any], reason: str) -> None:
    assert reason in (host_reach(body) or "")


@pytest.mark.parametrize("body", SAFE)
def test_a_setting_that_does_not_reach_the_host_passes_whatever_its_key(body: Dict[str, Any]) -> None:
    assert host_reach(body) is None


# -- /proxy create_instance sends params as the instance body (6bcae64) --------------------------


@pytest.mark.parametrize(
    "params,reason",
    [
        ({"name": "vm", "source": {"type": "none"}, "config": {"security.privileged": "true"}}, "security.privileged"),
        ({"name": "vm", "source": {"type": "copy", "source": "x", "project": "tenant-b"}}, "tenant-b"),
        ({"name": "vm", "source": {"type": "none"}, "devices": {"h": {"type": "disk", "source": "/", "path": "/h"}}},
         "host path"),
    ],
)
async def test_proxy_create_holds_a_narrow_key_to_the_body_rule(monkeypatch, params: Dict[str, Any],
                                                               reason: str) -> None:
    sent: List[Any] = []
    monkeypatch.setattr(integrations, "execute_proxy_operation", lambda *a, **_k: sent.append(a) or {"ok": True})
    # note: exec and files are granted so the body rule is what refuses, not the guest-capability price
    narrow = key(["tenant-a"], capabilities=["lxd.proxy", "instance.exec", "instance.files"])
    with pytest.raises(HTTPException) as caught:
        await proxy_operation(
            request=ProxyRequest(cluster_id=CLUSTER_ID, operation="create_instance", project="tenant-a", params=params),
            api_key=narrow, lxd_service=SimpleNamespace(), _=None,
        )
    assert caught.value.status_code == 403 and reason in caught.value.detail
    assert sent == [], "the refused body reached the cluster"


async def test_proxy_create_still_works_inside_the_grant_and_for_a_star_key(monkeypatch) -> None:
    sent: List[Any] = []
    monkeypatch.setattr(integrations, "execute_proxy_operation", lambda *a, **_k: sent.append(a[2]) or {"ok": True})
    monkeypatch.setattr(integrations, "proxy_write_reach", lambda *_a: None)
    plain = {"name": "vm", "source": {"type": "none"}}
    answer = await proxy_operation(
        request=ProxyRequest(cluster_id=CLUSTER_ID, operation="create_instance", project="tenant-a", params=plain),
        api_key=NARROW, lxd_service=SimpleNamespace(), _=None,
    )
    assert answer.success is True
    privileged = dict(plain, config={"security.privileged": "true"})
    answer = await proxy_operation(
        request=ProxyRequest(cluster_id=CLUSTER_ID, operation="create_instance", project="tenant-a",
                             params=privileged),
        api_key=key(["*"], capabilities=["lxd.proxy", "instance.exec"]), lxd_service=SimpleNamespace(), _=None,
    )
    assert answer.success is True and sent == ["create_instance", "create_instance"]
