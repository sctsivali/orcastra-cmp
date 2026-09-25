"""
A raw-proxy request that hands a guest's data out costs `instance.files`.

A POST with `migration: true` on an instance, an instance snapshot or a custom volume answers with
an operation whose control, fs and criu secrets stream the filesystem to anyone who presents them
(and a `target` pushes it to a server the caller names). GET /1.0/images/<fp>/export returns the
image tarball, and POST /1.0/images/<fp>/secret lets an untrusted client download it. A write key
without `instance.files` could do all of it, a `*` key included, while reading one file through
/files cost that capability.

The integrator's own write paths never send one of these bodies (its client source was read), so
each of them is pinned as still sent for a key holding its preset. That includes its marketplace
publish (POST /1.0/images from a snapshot), which this rule does not price.

Every raw-proxy test drives the real handler; the session records the request it would send.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

import pytest

from app.core.api_key_capabilities import APP_TYPE_CAPABILITIES
from app.core.guest_data_export import guest_data_capability_for
from app.mcp.contract import TOOL_CONTRACT
from app.routers import integrations
from app.services.lxd.proxy_operations import GUEST_ACCESS_OPERATIONS, PROXY_OPERATION_LEVELS
from tests._raw_proxy_fakes import (  # noqa: F401 - fixtures are registered by import
    CLUSTER_ID,
    ENDPOINT,
    ORCAHUB_READS,
    ORCAHUB_WRITES,
    _clean_breaker,
    call_raw_proxy,
    fence_calls,
)

FP = "0f3c9a1b2c3d4e5f"
MIGRATE = {"name": "web-1", "migration": True, "live": False}

PRICED: List[Tuple[str, str, Optional[Dict[str, Any]]]] = [
    ("POST", "/1.0/instances/web-1", MIGRATE),
    ("POST", "/1.0/instances/web-1?target=node-2", {"migration": True}),
    ("POST", "/1.0/containers/web-1", {"migration": True}),
    ("POST", "/1.0/virtual-machines/web-1", {"migration": True, "live": True}),
    ("POST", "/1.0/instances/web-1", {"migration": True, "target": {"certificate": "x", "websockets": {}}}),
    ("POST", "/1.0/instances/web-1/snapshots/snap0", {"name": "snap0", "migration": True}),
    ("POST", "/1.0/storage-pools/default/volumes/custom/vol1", {"name": "vol1", "migration": True}),
    ("POST", "/1.0/storage-pools/default/volumes/custom/vol1/snapshots/s0", {"migration": True}),
    ("POST", "/1.0/storage-pools/default/volumes/virtual-machine/web-1", {"migration": True}),
    # LXD decodes a bool, so a value it would refuse is priced rather than guessed at.
    ("POST", "/1.0/instances/web-1", {"migration": "yes"}),
    ("POST", "/1.0/instances/web-1", {"migration": 1}),
    ("GET", f"/1.0/images/{FP}/export", None),
    ("GET", f"/1.0/images/{FP}/export?secret=abc", None),
    ("POST", f"/1.0/images/{FP}/secret", {}),
    # Spellings the canonical parser reads as the same request.
    ("GET", f"/1.0/images/{FP}/%65xport", None),
    ("GET", f"/1.0/IMAGES/{FP}/EXPORT/", None),
    ("POST", f"/1.0/images/{FP}/%2573ecret", {}),
    ("POST", "/1.0/%69nstances/web-1", {"migration": True}),
    # LXD routes images/{fingerprint}/export before the alias route.
    ("GET", "/1.0/images/aliases/export", None),
]

NOT_PRICED: List[Tuple[str, str, Optional[Dict[str, Any]]]] = [
    ("POST", "/1.0/instances/web-1", {"name": "web-2"}),
    ("POST", "/1.0/instances/web-1", {"name": "web-2", "migration": False}),
    ("POST", "/1.0/instances/web-1", {"name": "web-2", "migration": None}),
    ("PUT", "/1.0/instances/web-1", {"migration": True}),
    ("POST", "/1.0/instances", {"name": "web-3", "source": {"type": "copy", "source": "web-1"}}),
    ("POST", "/1.0/instances/web-1/snapshots", {"name": "snap1"}),
    ("POST", "/1.0/storage-pools/default/volumes/custom", {"name": "vol2", "migration": True}),
    ("GET", f"/1.0/images/{FP}", None),
    ("GET", "/1.0/images/aliases/mkt/org/export", None),
    ("POST", f"/1.0/images/{FP}/refresh", {}),
    ("GET", "/1.0/instances/export", None),
    ("GET", "/1.0/instances/web-1/snapshots/export", None),
]

# The integrator's write bodies, as its client source builds them.
ORCAHUB_BODIES: Dict[Tuple[str, str], List[Dict[str, Any]]] = {
    ("POST", "/1.0/images"): [
        {
            "source": {"type": "image", "mode": "pull", "protocol": "simplestreams",
                       "server": "https://images.example.test", "alias": "ubuntu/24.04", "image_type": "container"},
            "aliases": [{"name": "ubuntu/24.04"}], "public": False, "auto_update": True,
        },
        {
            "source": {"type": "snapshot", "name": "web-1/mktimg-a1b2"},
            "aliases": [{"name": "mkt/org-7/app", "description": "App"}],
            "public": False, "compression_algorithm": "gzip",
        },
    ],
    ("POST", "/1.0/images/aliases"): [{"name": "ubuntu/24.04", "target": FP}],
    ("POST", "/1.0/instances"): [
        {"name": "web-9", "type": "container", "source": {"type": "image", "alias": "ubuntu/24.04"},
         "profiles": ["default"], "config": {"limits.cpu": "2"}, "devices": {}},
    ],
    ("PUT", "/1.0/instances/web-1"): [{"restore": "snap0"}, {"config": {}, "devices": {}, "profiles": ["default"]}],
    ("PATCH", "/1.0/instances/web-1"): [{"config": {"limits.memory": "2GiB"}}],
    ("PUT", "/1.0/instances/web-1/state"): [{"action": "start", "timeout": 30, "force": False}],
    ("POST", "/1.0/instances/web-1/snapshots"): [{"name": "snap1", "stateful": False, "expires_at": "2026-09-26T00:00:00Z"}],
    ("POST", "/1.0/projects"): [{"name": "cust-buyer-0f3c9a", "config": {"features.images": "false"}}],
    ("PATCH", "/1.0/projects/cust-buyer-0f3c9a"): [{"config": {"limits.cpu": "4"}}],
    ("POST", "/1.0/storage-pools/default/volumes/custom"): [
        {"name": "data-1", "content_type": "filesystem", "config": {"size": "10GiB"}}
    ],
    ("PUT", "/1.0/storage-pools/default/volumes/custom/data-1"): [{"restore": "snap0"}],
    ("PATCH", "/1.0/storage-pools/default/volumes/custom/data-1"): [{"config": {"size": "20GiB"}}],
    ("PUT", "/1.0/storage-pools/default/volumes/custom/data-1/state"): [{}],
    ("POST", "/1.0/storage-pools/default/volumes/custom/data-1/snapshots"): [{"name": "snap1"}],
}


def _key(capabilities: Optional[List[str]], projects: Tuple[str, ...] = ("*",)) -> Dict[str, Any]:
    api_key: Dict[str, Any] = {
        "id": "oak_guest_data",
        "app_name": "qa-guest-data",
        "cluster_permissions": [
            {"cluster_id": CLUSTER_ID, "permissions": ["read", "write", "admin"], "projects": list(projects)}
        ],
    }
    if capabilities is not None:
        api_key["capabilities"] = capabilities
    return api_key


ORCAHUB = _key(sorted(APP_TYPE_CAPABILITIES["orcahub"]))
WRITE_NO_FILES = _key(["inventory.read", "lxd.proxy", "instance.exec"])
NARROW_NO_FILES = _key(["inventory.read", "lxd.proxy"], projects=("tenant-a",))
WITH_FILES = _key(["inventory.read", "lxd.proxy", "instance.files"])


@pytest.fixture
def audits(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    """What raw_proxy writes to the audit trail, as the real _audit_key_action receives it."""
    recorded: List[Dict[str, Any]] = []
    monkeypatch.setattr(integrations, "_audit_key_action", lambda api_key, **fields: recorded.append(fields))
    return recorded


@pytest.mark.parametrize("method,path,body", PRICED)
def test_a_request_that_hands_out_guest_data_costs_files(method: str, path: str, body: Any) -> None:
    assert guest_data_capability_for(path, method, body) == "instance.files"


@pytest.mark.parametrize("method,path,body", NOT_PRICED)
def test_a_request_that_keeps_the_data_inside_is_not_priced(method: str, path: str, body: Any) -> None:
    assert guest_data_capability_for(path, method, body) is None


@pytest.mark.usefixtures("fence_calls")
@pytest.mark.parametrize("api_key", [WRITE_NO_FILES, NARROW_NO_FILES, ORCAHUB], ids=["star", "narrow", "orcahub"])
@pytest.mark.parametrize("method,path,body", [case for case in PRICED if "tenant" not in case[1]])
async def test_the_raw_proxy_refuses_it_without_files(
    method: str, path: str, body: Any, api_key: Dict[str, Any], audits: List[Dict[str, Any]]
) -> None:
    outcome, session = await call_raw_proxy(api_key, method, path, project=_project(api_key), body=body)

    assert getattr(outcome, "status_code", None) == 403, outcome
    assert "instance.files" in outcome.detail
    assert session.calls == []
    assert [(record["result"], record["details"]["refused"]) for record in audits] == [
        ("failure", "capability_not_granted")
    ]


def _project(api_key: Dict[str, Any]) -> Optional[str]:
    projects = api_key["cluster_permissions"][0]["projects"]
    return None if "*" in projects else projects[0]


@pytest.mark.usefixtures("fence_calls")
@pytest.mark.parametrize("method,path,body", PRICED[:9] + PRICED[11:14])
async def test_a_key_granted_files_sends_it_as_asked(
    method: str, path: str, body: Any, audits: List[Dict[str, Any]]
) -> None:
    outcome, session = await call_raw_proxy(WITH_FILES, method, path, body=body)

    assert outcome.status_code == 200, outcome
    assert len(session.calls) == 1
    verb, url, kwargs = session.calls[0]
    assert verb == method
    assert url.startswith(ENDPOINT)
    assert kwargs.get("json") == body


async def test_a_legacy_key_never_inherits_it(audits: List[Dict[str, Any]]) -> None:
    outcome, session = await call_raw_proxy(_key(None), "GET", f"/1.0/images/{FP}/export")

    assert getattr(outcome, "status_code", None) == 403
    assert session.calls == []


def test_every_integrator_write_is_pinned_here() -> None:
    assert set(ORCAHUB_BODIES) == {(method, path) for method, path in ORCAHUB_WRITES if method != "DELETE"}


@pytest.mark.usefixtures("fence_calls")
@pytest.mark.parametrize(
    "method,path,body",
    [(method, path, body) for (method, path), bodies in ORCAHUB_BODIES.items() for body in bodies]
    + [(method, path, None) for method, path in ORCAHUB_WRITES if method == "DELETE"],
)
async def test_the_integrators_writes_are_still_sent(
    method: str, path: str, body: Any, audits: List[Dict[str, Any]]
) -> None:
    assert guest_data_capability_for(path, method, body) is None

    outcome, session = await call_raw_proxy(ORCAHUB, method, path, body=body)

    assert outcome.status_code == 200, outcome
    assert [call[0] for call in session.calls] == [method]
    assert [record["result"] for record in audits] == ["success"]


@pytest.mark.parametrize("path", ORCAHUB_READS)
def test_the_integrators_reads_are_not_priced(path: str) -> None:
    assert guest_data_capability_for(path, "GET", None) is None


# ── ratchet: the raw proxy is the only door ────────────────────────────────────────────────────

_HANDS_OUT = re.compile(r"migrat|publish|export|secret|backup|copy|move|transfer|download")


def test_ratchet_no_proxy_operation_hands_out_guest_data_unpriced() -> None:
    """/proxy and MCP send none of these today. An operation added later that does costs files."""
    for operation in PROXY_OPERATION_LEVELS:
        if _HANDS_OUT.search(operation):
            assert GUEST_ACCESS_OPERATIONS.get(operation) == "instance.files", operation
    for tool, requirement in TOOL_CONTRACT.items():
        if _HANDS_OUT.search(tool):
            assert requirement.operation is not None, tool
            assert GUEST_ACCESS_OPERATIONS.get(requirement.operation) == "instance.files", tool
