"""
A key write carrying cloud-init user-data or vendor-data costs `instance.exec`, for every key.

LXD hands `cloud-init.user-data`, `cloud-init.vendor-data`, `user.user-data` and `user.vendor-data`
to cloud-init in the guest, and gives the guest a new cloud-init instance-id when one of them changes
(needsNewInstanceID, lxd/instance/drivers/driver_common.go), so the next boot applies the new data.
The rule reads key presence only, so every value here is an inert placeholder.

Only these four keys are new for every key: raw.*, security.privileged and host devices keep today's
rule on each surface, so the integrator's GPU recipe still goes through on a `*` key. The
integrator's instance and project writes (its client source was read, and it sends no cloud-init
key) are pinned as still sent.
"""

import inspect
from typing import Any, Dict, List, Tuple

import pytest
from fastapi import HTTPException

from app.core.api_key_capabilities import APP_TYPE_CAPABILITIES
from app.core.cloud_init_config import CLOUD_INIT_KEYS, cloud_init_capability_for, proxy_cloud_init_capability_for
from app.mcp import tools_write
from app.mcp.contract import TOOL_CONTRACT
from app.routers import integrations
from app.services.lxd.proxy_operations import PROXY_OPERATION_LEVELS
from tests._limited_write_fakes import (  # noqa: F401 - fixtures are registered by import
    FakeClient,
    ProjectSession,
    ToolRegistry,
    _no_webhook_events,
    mcp_call,
    proxy_call,
)
from tests._raw_proxy_fakes import _clean_breaker, call_raw_proxy, fence_calls, key  # noqa: F401

PLACEHOLDER = "#cloud-config\n"
IMAGE = {"type": "image", "alias": "ubuntu/24.04"}

PRICED: List[Tuple[str, str, Dict[str, Any]]] = [
    ("POST", "/1.0/instances", {"name": "c1", "source": IMAGE, "config": {"cloud-init.user-data": PLACEHOLDER}}),
    ("POST", "/1.0/instances", {"name": "c1", "source": {"type": "copy", "source": "web-1"},
                                "config": {"user.user-data": PLACEHOLDER}}),
    ("POST", "/1.0/instances", {"name": "c1", "source": {"type": "none"},
                                "config": {"cloud-init.vendor-data": PLACEHOLDER}}),
    ("POST", "/1.0/containers", {"name": "c1", "source": IMAGE, "config": {"user.vendor-data": PLACEHOLDER}}),
    ("POST", "/1.0/virtual-machines", {"name": "v1", "source": IMAGE, "config": {"cloud-init.user-data": ""}}),
    ("PUT", "/1.0/instances/web-1", {"config": {"user.user-data": PLACEHOLDER}, "devices": {}, "profiles": ["default"]}),
    ("PATCH", "/1.0/instances/web-1", {"config": {"cloud-init.user-data": PLACEHOLDER}}),
    ("PATCH", "/1.0/virtual-machines/web-1", {"config": {"cloud-init.vendor-data": PLACEHOLDER}}),
    ("POST", "/1.0/instances/web-1", {"name": "web-2", "config": {"cloud-init.user-data": PLACEHOLDER}}),
    ("POST", "/1.0/profiles", {"name": "p1", "config": {"cloud-init.user-data": PLACEHOLDER}}),
    ("PUT", "/1.0/profiles/default", {"config": {"user.user-data": PLACEHOLDER}, "devices": {}}),
    ("PATCH", "/1.0/profiles/default", {"config": {"cloud-init.vendor-data": ""}}),
    # Key names are compared without regard to case, and paths are read by the canonical parser.
    ("PATCH", "/1.0/instances/web-1", {"config": {"Cloud-Init.User-Data": PLACEHOLDER}}),
    ("PATCH", "/1.0/instances/web-1", {"config": {"USER.VENDOR-DATA": PLACEHOLDER}}),
    ("PATCH", "/1.0/%69nstances/web-1", {"config": {"cloud-init.user-data": PLACEHOLDER}}),
    ("PUT", "/1.0/PROFILES/default/", {"config": {"cloud-init.user-data": PLACEHOLDER}}),
]
PRICED_IDS = [
    "create-image", "create-copy", "create-none", "create-containers", "create-vm-empty-value", "put",
    "patch", "patch-vm", "move-with-config", "profile-create", "profile-put", "profile-patch-empty",
    "key-mixed-case", "key-upper", "encoded-path", "profile-upper-path",
]

NOT_PRICED: List[Tuple[str, str, Dict[str, Any]]] = [
    ("PATCH", "/1.0/instances/web-1", {"config": {"cloud-init.network-config": PLACEHOLDER}}),
    ("PATCH", "/1.0/instances/web-1", {"config": {"volatile.cloud-init.instance-id": "0f3c"}}),
    ("PATCH", "/1.0/instances/web-1", {"config": {"user.user-data-note": "x", "user.description": "x"}}),
    ("POST", "/1.0/instances/web-1/snapshots", {"name": "s1", "config": {"cloud-init.user-data": PLACEHOLDER}}),
    ("PUT", "/1.0/instances/web-1/state", {"action": "start", "config": {"cloud-init.user-data": PLACEHOLDER}}),
    ("POST", "/1.0/profiles/default", {"name": "renamed", "config": {"cloud-init.user-data": PLACEHOLDER}}),
    ("POST", "/1.0/projects", {"name": "p", "config": {"user.user-data": PLACEHOLDER}}),
    ("POST", "/1.0/storage-pools/default/volumes/custom", {"name": "v", "config": {"user.user-data": PLACEHOLDER}}),
    ("GET", "/1.0/instances/web-1", {"config": {"cloud-init.user-data": PLACEHOLDER}}),
    ("PATCH", "/1.0/instances/web-1", {"config": "cloud-init.user-data"}),
]
NOT_PRICED_IDS = [
    "network-config", "volatile-instance-id", "user-other", "snapshot-create", "state", "profile-rename",
    "project", "volume", "read", "config-not-a-map",
]

# The integrator's instance and project writes, as its client source builds them.
ORCAHUB_ROUND_TRIP_CONFIG = {
    "image.architecture": "arm64",
    "image.os": "Ubuntu",
    "limits.cpu": "2",
    "limits.memory": "2GiB",
    "environment.LD_LIBRARY_PATH": "/usr/lib/aarch64-linux-gnu/tegra:/usr/lib/aarch64-linux-gnu/nvidia",
    "volatile.base_image": "0f3c9a1b2c3d4e5f",
    "volatile.cloud-init.instance-id": "5b1e0a4c-0000-4000-8000-000000000001",
    "volatile.eth0.hwaddr": "00:16:3e:00:00:01",
    "volatile.last_state.power": "STOPPED",
    "volatile.uuid": "5b1e0a4c-0000-4000-8000-000000000002",
}
GPU_DEVICES = {
    "dev-nvhost-gpu": {"type": "unix-char", "source": "/dev/nvhost-gpu", "path": "/dev/nvhost-gpu"},
    "lib-usr-lib-aarch64-linux-gnu-tegra": {"type": "disk", "source": "/usr/lib/aarch64-linux-gnu/tegra",
                                            "path": "/usr/lib/aarch64-linux-gnu/tegra", "readonly": "true"},
}
ROOT_DEVICES = {"root": {"type": "disk", "path": "/", "pool": "default", "size": "20GiB"},
                "eth0": {"name": "eth0", "type": "nic", "network": "lxdbr0"}}
EDITABLE = {"architecture": "aarch64", "config": ORCAHUB_ROUND_TRIP_CONFIG, "devices": {**ROOT_DEVICES, **GPU_DEVICES},
            "profiles": ["default"], "description": "Plan - Order 1", "ephemeral": False, "stateful": False}
ORCAHUB_WRITES: List[Tuple[str, str, Dict[str, Any]]] = [
    ("POST", "/1.0/instances", {"name": "web-9", "type": "container", "source": IMAGE, "profiles": ["default"],
                                "config": {"limits.cpu": "2", "limits.memory": "2GiB",
                                           "environment.LD_LIBRARY_PATH": ORCAHUB_ROUND_TRIP_CONFIG[
                                               "environment.LD_LIBRARY_PATH"]},
                                "devices": {**ROOT_DEVICES, **GPU_DEVICES}, "description": "Plan - Order 1"}),
    ("POST", "/1.0/instances", {"name": "vm-9", "type": "virtual-machine", "source": IMAGE, "profiles": ["default"],
                                "config": {"limits.cpu": "2", "limits.memory": "4GiB", "boot.mode": "uefi-nosecureboot"},
                                "devices": ROOT_DEVICES}),
    ("PUT", "/1.0/instances/web-9", EDITABLE),
    ("PUT", "/1.0/instances/web-9", {"restore": "snap0"}),
    ("PATCH", "/1.0/instances/vm-9", {"config": {"boot.mode": "uefi-nosecureboot"}}),
    ("PATCH", "/1.0/instances/vm-9", {"config": {"security.secureboot": "false"}}),
    ("PATCH", "/1.0/instances/web-9", {"config": {"snapshots.schedule": "@daily", "snapshots.expiry": "7d",
                                                  "snapshots.pattern": "snap-%d"}}),
    ("PATCH", "/1.0/instances/web-9", {"devices": {"data-1": {"type": "disk", "pool": "default",
                                                              "source": "data-1", "path": "/mnt/data"}}}),
    ("POST", "/1.0/projects", {"name": "cust-buyer-0f3c9a", "description": "",
                               "config": {"features.images": "false", "features.profiles": "false",
                                          "features.storage.volumes": "false", "features.networks": "false",
                                          "user.description": "buyer"}}),
    ("PATCH", "/1.0/projects/cust-buyer-0f3c9a", {"config": {"limits.cpu": "4", "limits.memory": "8GiB"}}),
]
ORCAHUB_IDS = ["create-gpu", "create-vm", "put-editable", "restore", "patch-boot-mode", "patch-secureboot",
               "patch-snapshot-schedule", "patch-volume-attach", "project-create", "project-limits"]

STAR_NO_EXEC = key(["*"], capabilities=["inventory.read", "lxd.proxy", "instance.files"])
NARROW_NO_EXEC = key(["tenant-a"], capabilities=["inventory.read", "lxd.proxy"])
ORCAHUB = key(["*"], capabilities=sorted(APP_TYPE_CAPABILITIES["orcahub"]))
WITH_EXEC = key(["*"], capabilities=["inventory.read", "lxd.proxy", "instance.exec"])
WITH_BOTH = key(["*"], capabilities=["inventory.read", "lxd.proxy", "instance.exec", "instance.files"])


@pytest.fixture
def audits(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    """What the handlers write to the audit trail, as the real _audit_key_action receives it."""
    recorded: List[Dict[str, Any]] = []
    monkeypatch.setattr(integrations, "_audit_key_action", lambda api_key, **fields: recorded.append(fields))
    return recorded


def _failures(audits: List[Dict[str, Any]]) -> List[Tuple[str, str]]:
    return [(record["result"], record["details"]["refused"]) for record in audits if record["result"] == "failure"]


# -- the raw proxy ---------------------------------------------------------------------------------


@pytest.mark.parametrize("method,path,body", PRICED, ids=PRICED_IDS)
def test_a_write_carrying_cloud_init_data_costs_exec(method: str, path: str, body: Dict[str, Any]) -> None:
    assert cloud_init_capability_for(path, method, body) == "instance.exec"


@pytest.mark.parametrize("method,path,body", NOT_PRICED, ids=NOT_PRICED_IDS)
def test_other_config_and_other_writes_are_not_priced(method: str, path: str, body: Dict[str, Any]) -> None:
    assert cloud_init_capability_for(path, method, body) is None


@pytest.mark.usefixtures("fence_calls")
@pytest.mark.parametrize("api_key", [STAR_NO_EXEC, ORCAHUB], ids=["star", "orcahub"])
@pytest.mark.parametrize("method,path,body", PRICED, ids=PRICED_IDS)
async def test_the_raw_proxy_refuses_it_without_exec_and_records_it(
    method: str, path: str, body: Dict[str, Any], api_key: Dict[str, Any], audits: List[Dict[str, Any]]
) -> None:
    outcome, session = await call_raw_proxy(api_key, method, path, body=body)

    assert isinstance(outcome, HTTPException) and outcome.status_code == 403, outcome
    assert "instance.exec" in outcome.detail
    assert session.calls == []
    assert _failures(audits) == [("failure", "capability_not_granted")]


@pytest.mark.usefixtures("fence_calls")
async def test_a_narrow_key_is_refused_too(audits: List[Dict[str, Any]]) -> None:
    outcome, session = await call_raw_proxy(NARROW_NO_EXEC, *PRICED[6][:2], project="tenant-a", body=PRICED[6][2])

    assert isinstance(outcome, HTTPException) and outcome.status_code == 403 and "instance.exec" in outcome.detail
    assert session.calls == []


async def test_a_legacy_key_never_inherits_it(audits: List[Dict[str, Any]]) -> None:
    outcome, session = await call_raw_proxy(key(["*"]), *PRICED[6][:2], body=PRICED[6][2])

    assert isinstance(outcome, HTTPException) and outcome.status_code == 403
    assert session.calls == []


@pytest.mark.usefixtures("fence_calls")
@pytest.mark.parametrize("method,path,body", PRICED[:12], ids=PRICED_IDS[:12])
async def test_a_key_granted_exec_sends_it_as_asked(
    method: str, path: str, body: Dict[str, Any], audits: List[Dict[str, Any]]
) -> None:
    outcome, session = await call_raw_proxy(WITH_EXEC, method, path, body=body)

    assert outcome.status_code == 200, outcome
    assert [(verb, kwargs.get("json")) for verb, _url, kwargs in session.calls] == [(method, body)]


@pytest.mark.usefixtures("fence_calls")
@pytest.mark.parametrize("method,path,body", ORCAHUB_WRITES, ids=ORCAHUB_IDS)
async def test_the_integrators_config_writes_are_still_sent(
    method: str, path: str, body: Dict[str, Any], audits: List[Dict[str, Any]]
) -> None:
    assert cloud_init_capability_for(path, method, body) is None

    outcome, session = await call_raw_proxy(ORCAHUB, method, path, body=body)

    assert outcome.status_code == 200, outcome
    assert [(verb, kwargs.get("json")) for verb, _url, kwargs in session.calls] == [(method, body)]
    # note: the schedule write also leaves the policy observer's record, which is dev's
    assert _failures(audits) == [] and audits[-1]["result"] == "success"


@pytest.mark.usefixtures("fence_calls")
@pytest.mark.parametrize(
    "body",
    [
        {"config": {"raw.lxc": "lxc.apparmor.profile = unconfined"}},
        {"config": {"security.privileged": "true", "security.nesting": "true"}},
        {"devices": {"gpu": {"type": "unix-char", "source": "/dev/nvhost-gpu", "path": "/dev/nvhost-gpu"}}},
        {"devices": {"host": {"type": "disk", "source": "/srv/data", "path": "/mnt/data"}}},
    ],
    ids=["raw-lxc", "privileged", "unix-char", "host-disk"],
)
async def test_a_star_key_keeps_todays_rule_for_host_reaching_config(
    body: Dict[str, Any], audits: List[Dict[str, Any]]
) -> None:
    """The host-reach rule for a `*` key waits for the party oracle, so none of these is new."""
    outcome, session = await call_raw_proxy(STAR_NO_EXEC, "PATCH", "/1.0/instances/web-1", body=body)

    assert outcome.status_code == 200, outcome
    assert [verb for verb, *_ in session.calls] == ["PATCH"]


@pytest.mark.usefixtures("fence_calls")
async def test_an_unchanged_value_carried_by_a_round_trip_is_priced_too(audits: List[Dict[str, Any]]) -> None:
    """A PUT carries the whole editable config, so an instance already holding user-data costs exec to
    rewrite. Known limit, recorded for the owner: LXD would not run anything for an unchanged value."""
    body = {**EDITABLE, "config": {**ORCAHUB_ROUND_TRIP_CONFIG, "user.user-data": PLACEHOLDER}}
    outcome, session = await call_raw_proxy(ORCAHUB, "PUT", "/1.0/instances/web-9", body=body)

    assert isinstance(outcome, HTTPException) and outcome.status_code == 403
    assert session.calls == []


# -- /proxy, and MCP which calls the same handler ----------------------------------------------------


@pytest.mark.parametrize(
    "operation,params",
    [
        ("create_instance", {"name": "c1", "source": IMAGE, "config": {"cloud-init.user-data": PLACEHOLDER}}),
        ("update_instance", {"name": "web", "config": {"user.user-data": PLACEHOLDER}}),
        ("update_instance", {"name": "web", "config": {"Cloud-Init.Vendor-Data": PLACEHOLDER}}),
    ],
    ids=["create", "update", "update-mixed-case"],
)
async def test_proxy_refuses_it_without_exec_and_records_it(
    operation: str, params: Dict[str, Any], audits: List[Dict[str, Any]]
) -> None:
    assert proxy_cloud_init_capability_for(operation, params) == "instance.exec"

    outcome, client = await proxy_call(operation, "default", params, api_key=STAR_NO_EXEC)

    assert isinstance(outcome, HTTPException) and outcome.status_code == 403 and "instance.exec" in outcome.detail
    assert client.sent == [] and client.api.session.calls == []
    assert _failures(audits) == [("failure", "capability_not_granted")]


async def test_a_proxy_refusal_is_attributed_to_the_clusters_organization(
    monkeypatch: pytest.MonkeyPatch, audits: List[Dict[str, Any]]
) -> None:
    """The refusal takes the guest-capability path, which resolves the owning organization first."""
    from app.core import database, org_scope

    class _Session:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_: Any) -> None:
            return None

    async def _resolve(cluster_id: str, _db: Any) -> Tuple[int, str]:
        return 7, "org-seven"

    monkeypatch.setattr(database, "async_session_maker", lambda: _Session())
    monkeypatch.setattr(org_scope, "resolve_cluster_org", _resolve)

    outcome, _ = await proxy_call("update_instance", "default",
                                  {"name": "web", "config": {"cloud-init.user-data": PLACEHOLDER}},
                                  api_key=STAR_NO_EXEC)

    assert isinstance(outcome, HTTPException) and outcome.status_code == 403
    assert [(record["target_organization_id"], record["target_organization_slug"]) for record in audits] == [
        (7, "org-seven")
    ]
    assert audits[0]["action"] == "integration.proxy.request"


async def test_proxy_asks_for_exec_on_top_of_files_for_a_host_disk(audits: List[Dict[str, Any]]) -> None:
    params = {"name": "web", "config": {"cloud-init.user-data": PLACEHOLDER},
              "devices": {"host": {"type": "disk", "source": "/srv/data", "path": "/mnt/data"}}}

    files_only, client = await proxy_call("update_instance", "default", params, api_key=STAR_NO_EXEC)
    assert isinstance(files_only, HTTPException) and files_only.status_code == 403
    assert "instance.exec" in files_only.detail and client.sent == []
    assert audits[-1]["details"]["cloud_init_keys"] == ["cloud-init.user-data"]

    exec_only, client = await proxy_call("update_instance", "default", params, api_key=WITH_EXEC)
    assert isinstance(exec_only, HTTPException) and "instance.files" in exec_only.detail and client.sent == []

    both, client = await proxy_call("update_instance", "default", params, api_key=WITH_BOTH)
    assert not isinstance(both, HTTPException) and both.success, both
    assert [verb for verb, *_ in client.sent] == ["save"]


@pytest.mark.parametrize(
    "operation,params",
    [
        ("create_instance", {"name": "c1", "source": IMAGE, "config": {"cloud-init.user-data": PLACEHOLDER}}),
        ("update_instance", {"name": "web", "config": {"user.vendor-data": PLACEHOLDER}}),
    ],
    ids=["create", "update"],
)
async def test_proxy_sends_it_for_a_key_granted_exec(operation: str, params: Dict[str, Any]) -> None:
    outcome, client = await proxy_call(operation, "default", params, api_key=WITH_EXEC)

    assert not isinstance(outcome, HTTPException) and outcome.success, outcome
    assert [verb for verb, *_ in client.sent] == ["create" if operation == "create_instance" else "save"]


async def test_proxy_config_without_cloud_init_is_unchanged() -> None:
    outcome, client = await proxy_call("update_instance", "default",
                                       {"name": "web", "config": {"limits.cpu": "2", "user.description": "x"}},
                                       api_key=STAR_NO_EXEC)

    assert not isinstance(outcome, HTTPException) and outcome.success, outcome
    assert [verb for verb, *_ in client.sent] == ["save"]


@pytest.mark.parametrize("tool", ["lxd_create_instance", "lxd_update_instance"])
async def test_mcp_refuses_it_without_exec(tool: str, monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(ProjectSession())
    result = await mcp_call(tools_write, tool, client, STAR_NO_EXEC, monkeypatch, cluster_id="cluster-a",
                            name="web", project="default", config={"cloud-init.user-data": PLACEHOLDER})

    assert result.is_error is True and "instance.exec" in result.content[0].text
    assert client.sent == []


async def test_mcp_sends_it_for_a_key_granted_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(ProjectSession())
    result = await mcp_call(tools_write, "lxd_update_instance", client, WITH_EXEC, monkeypatch,
                            cluster_id="cluster-a", name="web", project="default",
                            config={"cloud-init.user-data": PLACEHOLDER})

    assert result.is_error is False, result.content
    assert [verb for verb, *_ in client.sent] == ["save"]


# -- ratchets: every /proxy and MCP door that writes instance or profile config is priced --------------


def test_ratchet_no_proxy_operation_writes_a_profile() -> None:
    """/proxy lists profiles and never writes one. An operation added that does must be priced here."""
    writes = [op for op, level in PROXY_OPERATION_LEVELS.items() if "profile" in op and level != "read"]
    assert writes == []


def test_ratchet_every_mcp_tool_that_takes_config_is_priced() -> None:
    registry = ToolRegistry()
    tools_write.register(registry)
    takes_config = [name for name, function in registry.tools.items()
                    if "config" in inspect.signature(function).parameters]
    assert takes_config, "no MCP write tool takes config, so update this ratchet"
    for name in takes_config:
        operation = TOOL_CONTRACT[name].operation
        assert proxy_cloud_init_capability_for(operation, {"config": {"user.user-data": "x"}}) == "instance.exec", name


def test_the_four_keys_are_the_ones_lxd_hands_to_cloud_init() -> None:
    assert CLOUD_INIT_KEYS == {"cloud-init.user-data", "cloud-init.vendor-data", "user.user-data", "user.vendor-data"}
