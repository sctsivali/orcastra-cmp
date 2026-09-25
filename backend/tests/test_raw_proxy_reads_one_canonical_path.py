"""
Every raw-proxy guard and the URL raw_proxy sends read one canonical path.

Each guard used to parse the raw string its own way (decode once or twice, split on `?` only,
refuse `..` only, strip the string prefix `/1.0`), while requests (urllib3 2.x) removes `.`
segments and never sends a fragment, and LXD decodes once. So the guards judged one request and
LXD served another:

| sent                                         | before                    | LXD served            |
|----------------------------------------------|---------------------------|-----------------------|
| /1.0/certificates#x                          | allowed                   | the trust store       |
| /1.0/./certificates                          | allowed                   | the trust store       |
| /1.0/auth/identities/tls                     | allowed                   | a trusted TLS client  |
| /1.0/instances/c1/./exec                     | allowed, no capability    | exec as root          |
| /1.0/instances/c1/exec#                      | allowed, no capability    | exec as root          |
| /1.0/instances/c1/./files?path=/etc/shadow   | allowed, no capability    | the guest's files     |
| /1.0/instances?%2570roject=b                 | project popped once       | project b             |

The migration fence read the path a third way and skipped a path with a fragment, so
`PUT /1.0/instances/web-01/state#x` resumed a fenced source.
"""

from typing import Any, Dict, List, Tuple

import pytest
from fastapi import HTTPException

from app.core.api_key_capabilities import (
    assert_proxy_path_allowed,
    guest_access_capability_for,
    policy_keys_in_raw_body,
    proxy_destructive_action_for,
    proxy_path_segments,
)
from app.core.raw_proxy_path import canonical_proxy_path, fully_decoded
from app.routers import integrations
from tests._raw_proxy_fakes import (  # noqa: F401 - fixtures are registered by import
    ENDPOINT,
    NARROW,
    ORCAHUB_READS,
    ORCAHUB_WRITES,
    WILDCARD,
    _clean_breaker,
    call_raw_proxy,
    fence_calls,
    key,
    lxd_reads,
)

LEGACY_WRITE = key(["*"])


# -- the canonical form ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,segments",
    [
        ("/1.0/instances", ("instances",)),
        ("/instances", ("instances",)),
        ("instances", ("instances",)),
        ("1.0/instances", ("instances",)),
        ("/1.0/instances/", ("instances",)),
        ("/1.0", ()),
        ("/1.0/", ()),
        ("/", ()),
        ("", ()),
        ("/1.0/%69nstances/Web-1", ("instances", "Web-1")),
        ("/1.0/%2569nstances/web-1", ("instances", "web-1")),
        ("/1.0/%252569nstances/web-1", ("instances", "web-1")),
        ("/1.0/instances/web-1#x", ("instances", "web-1")),
        ("/1.0/instances/web-1?recursion=1#x", ("instances", "web-1")),
        ("/1.0certificates", ("1.0certificates",)),
        ("/1.0/instances/a%2Fb", ("instances", "a", "b")),
        ("/1.0/instances/snap-%25Y%25m0-%25H%25M", ("instances", "snap-%Y%m0-%H%M")),
    ],
)
def test_the_canonical_segments(path: str, segments: Tuple[str, ...]) -> None:
    assert canonical_proxy_path(path).segments == segments


@pytest.mark.parametrize(
    "path",
    [
        "/1.0/./certificates",
        "/1.0/%2e/certificates",
        "/1.0/%252e/certificates",
        "/1.0/instances/c1/./exec",
        "/1.0/instances/c1/%2E/exec",
        "/1.0/instances/../certificates",
        "/1.0/instances/%2e%2e/certificates",
        "/1.0//certificates",
        "//1.0/certificates",
        "/1.0/instances//web-1",
        "/1.0//",
        "//",
        "/1.0/instances\\web-1",
        "/1.0/instances%5cweb-1",
        "/1.0/certificates%23x",
        "/1.0/certificates%3Fx=1",
        "/1.0/instances/c1/files%3Fpath=/etc/shadow",
        "/1.0/instances?path=/etc/../shadow",
        "https://elsewhere.example/1.0/instances",
        "//elsewhere.example/1.0/instances",
        "/1.0/%" + "25" * 8 + "69nstances",
    ],
)
def test_a_dot_an_empty_segment_or_a_backslash_is_refused(path: str) -> None:
    with pytest.raises(HTTPException) as caught:
        canonical_proxy_path(path)
    assert caught.value.status_code == 400


def test_the_query_is_decoded_once_and_a_bare_name_is_dropped() -> None:
    canonical = canonical_proxy_path("/1.0/instances?recursion=1&x%26all-projects=true&flag&a+b=c%2Bd#p=q")
    assert canonical.query == (("recursion", "1"), ("x&all-projects", "true"), ("a b", "c+d"))


def test_decoding_repeats_until_stable() -> None:
    assert fully_decoded("%252570roject") == "project"
    assert fully_decoded("snap-%25Y") == "snap-%Y"


@pytest.mark.parametrize(
    "path,target",
    [
        ("/1.0/instances?recursion=1", "/instances?recursion=1&project=tenant-a"),
        ("/1.0/instances?project=tenant-b&recursion=1", "/instances?recursion=1&project=tenant-a"),
        ("/1.0/instances?%2570roject=tenant-b", "/instances?project=tenant-a"),
        ("/1.0/instances?Project=tenant-b", "/instances?project=tenant-a"),
        ("/1.0/instances?a%23=1", "/instances?a%23=1&project=tenant-a"),
        ("/1.0/instances#&project=tenant-b", "/instances?project=tenant-a"),
        ("/1.0/instances/ATLAS-Semanan/snapshots/snap-%25Y%25m0-%25H%25M",
         "/instances/ATLAS-Semanan/snapshots/snap-%25Y%25m0-%25H%25M?project=tenant-a"),
        ("/1.0", "/?project=tenant-a"),
    ],
)
def test_the_request_target_carries_only_the_authorised_project(path: str, target: str) -> None:
    assert canonical_proxy_path(path).request_target("tenant-a") == target


def test_the_default_project_is_left_implicit_as_before() -> None:
    assert canonical_proxy_path("/1.0/instances?project=victim").request_target("default") == "/instances"
    assert canonical_proxy_path("/1.0").request_target(None) == "/"


# -- every consumer reads the same result ------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/1.0/instances/c1/exec#", "instance.exec"),
        ("/1.0/instances/c1/exec#x", "instance.exec"),
        ("/1.0/instances/c1/%2565xec", "instance.exec"),
        ("/1.0/instances/c1/files#?path=/etc/shadow", "instance.files"),
        ("/1.0/instances/c1/logs/", "instance.exec"),
    ],
)
def test_the_guest_classifier_reads_the_canonical_path(path: str, expected: str) -> None:
    assert guest_access_capability_for(path) == expected


@pytest.mark.parametrize(
    "path", ["/1.0/instances/c1/./exec", "/1.0/instances/c1/./files?path=/etc/shadow", "/1.0/./certificates"]
)
def test_every_classifier_refuses_what_the_path_guard_refuses(path: str) -> None:
    """No classifier answers "nothing needed" for a spelling the guard would refuse."""
    for classify in (
        assert_proxy_path_allowed,
        guest_access_capability_for,
        proxy_path_segments,
        lambda p: proxy_destructive_action_for("DELETE", p),
        lambda p: policy_keys_in_raw_body(p, {"config": {"snapshots.schedule": "@daily"}}),
    ):
        with pytest.raises(HTTPException) as caught:
            classify(path)
        assert caught.value.status_code == 400


def test_the_policy_observer_sees_a_write_behind_a_fragment() -> None:
    body = {"config": {"snapshots.schedule": "@daily"}}
    assert policy_keys_in_raw_body("/1.0/instances/web-01#x", body) == ["snapshots.schedule"]
    assert policy_keys_in_raw_body("/1.0/%2569nstances/web-01", body) == ["snapshots.schedule"]


# -- the bypass table, through the endpoint ------------------------------------------------------


@pytest.mark.parametrize(
    "method,path,status",
    [
        ("GET", "/1.0/certificates#x", 403),
        ("POST", "/1.0/certificates#x", 403),
        ("GET", "/1.0/./certificates", 400),
        ("POST", "/1.0/auth/identities/tls", 403),
        ("GET", "/1.0/auth/identities/tls", 403),
        ("POST", "/1.0/./certificates", 400),
        ("POST", "/1.0/instances/c1/./exec", 400),
        ("POST", "/1.0/instances/c1/exec#", 403),
        ("POST", "/1.0/instances/c1/exec#x", 403),
        ("GET", "/1.0/instances/c1/./files?path=/etc/shadow", 400),
        ("GET", "/1.0/instances/c1/files#?path=/etc/shadow", 403),
        ("GET", "/1.0/instances/c1/%2566iles?path=/etc/shadow", 403),
    ],
)
async def test_every_bypass_is_refused_before_the_cluster(method: str, path: str, status: int) -> None:
    """A write key without instance.exec or instance.files, as the integrator's keys are."""
    outcome, session = await call_raw_proxy(LEGACY_WRITE, method, path)
    assert isinstance(outcome, HTTPException), outcome
    assert outcome.status_code == status
    assert session.calls == []


@pytest.mark.parametrize(
    "path",
    ["/1.0/instances?%2570roject=tenant-b", "/1.0/instances?%70roject=tenant-b", "/1.0/instances?a%23=1#&project=x"],
)
async def test_a_smuggled_project_never_reaches_lxd(path: str) -> None:
    outcome, session = await call_raw_proxy(NARROW, "GET", path, project="tenant-a")
    assert outcome.status_code == 200
    _, query = lxd_reads(session.urls[0])
    assert [value for name, value in query if name == "project"] == ["tenant-a"]


async def test_a_granted_exec_still_reaches_the_cluster_as_judged() -> None:
    granted = key(["*"], capabilities=["lxd.proxy", "instance.exec"])
    outcome, session = await call_raw_proxy(granted, "POST", "/1.0/instances/c1/exec#x")
    assert outcome.status_code == 200
    assert session.urls == [f"{ENDPOINT}/instances/c1/exec"]


async def test_a_wildcard_project_value_is_encoded_not_joined() -> None:
    """A `*` grant may name any project, and the name used to be joined into the query raw."""
    outcome, session = await call_raw_proxy(WILDCARD, "GET", "/1.0/instances", project="p&all-projects=true#")
    assert outcome.status_code == 200
    _, query = lxd_reads(session.urls[0])
    assert query == [("project", "p&all-projects=true#")]


# -- the migration fence reads the same path --------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/1.0/instances/web-01/state#x",
        "/1.0/instances/web-01/state?x=1#y",
        "/1.0/%69nstances/web-01/state",
        "/1.0/%2569nstances/web-01/state",
        "/instances/web-01/state/",
    ],
)
async def test_the_fence_is_asked_for_every_spelling_of_a_resume(
    path: str, fence_calls: List[Tuple[str, Dict[str, Any]]]
) -> None:
    outcome, session = await call_raw_proxy(WILDCARD, "PUT", path, project="tenant-a", body={"action": "start"})
    assert outcome.status_code == 200
    assert ("_assert_proxy_write_is_not_fenced",
            {"cluster_id": "cluster-a", "project": "tenant-a", "instance_name": "web-01"}) in fence_calls
    segments, query = lxd_reads(session.urls[0], "PUT")
    assert segments == ["instances", "web-01", "state"]
    assert [value for name, value in query if name == "project"] == ["tenant-a"]


@pytest.mark.parametrize(
    "path,project",
    [("/1.0/profiles/default#x", "tenant-a"), ("/1.0/projects/%2574arget-a", "target-a")],
)
async def test_the_project_fence_reads_the_canonical_path(
    path: str, project: str, fence_calls: List[Tuple[str, Dict[str, Any]]]
) -> None:
    outcome, _ = await call_raw_proxy(WILDCARD, "PATCH", path, project="tenant-a", body={"config": {}})
    assert outcome.status_code == 200
    assert ("_assert_proxy_project_write_is_not_fenced",
            {"cluster_id": "cluster-a", "project": project}) in fence_calls


async def test_the_cluster_fence_reads_the_canonical_path(fence_calls: List[Tuple[str, Dict[str, Any]]]) -> None:
    outcome, _ = await call_raw_proxy(WILDCARD, "PUT", "/1.0/%2573torage-pools/local#x", body={"config": {}})
    assert outcome.status_code == 200
    assert ("_assert_proxy_cluster_write_is_not_fenced", {"cluster_id": "cluster-a"}) in fence_calls


def test_the_fence_helpers_read_the_canonical_path() -> None:
    request = integrations.RawProxyRequest(
        cluster_id="cluster-a", method="PUT", path="/1.0/instances/web-01/state#x", body={"action": "start"}
    )
    assert integrations._raw_proxy_fenced_write_target(request) == "web-01"
    dotted = request.model_copy(update={"path": "/1.0/instances/./web-01/state"})
    with pytest.raises(HTTPException):
        integrations._raw_proxy_fenced_write_target(dotted)


# -- the integrator keeps working ----------------------------------------------------------------


@pytest.mark.parametrize("path", ORCAHUB_READS)
async def test_every_integrator_read_is_sent_as_judged(path: str) -> None:
    outcome, session = await call_raw_proxy(WILDCARD, "GET", path, project="cust-buyer-0f3c9a")
    assert outcome.status_code == 200
    canonical = canonical_proxy_path(path)
    segments, query = lxd_reads(session.urls[0])
    assert segments == (list(canonical.segments) or [""])
    assert query == [pair for pair in canonical.query if pair[0] != "project"] + [("project", "cust-buyer-0f3c9a")]


@pytest.mark.parametrize("method,path", ORCAHUB_WRITES)
async def test_every_integrator_write_is_sent_as_judged(
    method: str, path: str, fence_calls: List[Tuple[str, Dict[str, Any]]]
) -> None:
    outcome, session = await call_raw_proxy(WILDCARD, method, path, project="cust-buyer-0f3c9a")
    assert outcome.status_code == 200
    segments, _ = lxd_reads(session.urls[0], method)
    assert segments == list(canonical_proxy_path(path).segments)
