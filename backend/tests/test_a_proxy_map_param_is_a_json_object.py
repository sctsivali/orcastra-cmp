"""/proxy config, devices and each device entry are JSON objects, or the request is refused.

update_instance merges config and devices into the instance with dict.update, which also takes a
list of [key, value] pairs, and every guard (the instance.exec price, the limited-key host rule,
the policy-key observer) reads a dict and skips any other shape, so a pairs-shaped config would be
judged empty and reach LXD as the settings it carries. The shape is refused with 400 in the canonical
params step, before any guard reads the params, for every operation and for MCP, which calls the
same handler.
"""

from typing import Any, Dict

import pytest
from fastapi import HTTPException

from app.mcp import tools_write
from tests._limited_write_fakes import (  # noqa: F401 - fixtures are registered by import
    TWO,
    WILDCARD,
    FakeClient,
    ProjectSession,
    _no_webhook_events,
    mcp_call,
    proxy_call,
)
from tests._raw_proxy_fakes import _clean_breaker  # noqa: F401

HOST_DISK = {"type": "disk", "source": "/", "path": "/mnt/host"}


def _saved(client: FakeClient) -> list:
    return [body for verb, _name, body in client.sent if verb in ("save", "create")]


@pytest.mark.parametrize(
    "api_key,params,field",
    [
        (TWO, {"name": "web", "config": [["security.privileged", "true"]]}, "params.config"),
        (TWO, {"name": "web", "devices": [["host", HOST_DISK]]}, "params.devices"),
        (WILDCARD, {"name": "web", "config": [["raw.lxc", "lxc.hook.pre-start = /bin/sh"]]}, "params.config"),
        (WILDCARD, {"name": "web", "devices": [["host", HOST_DISK]]}, "params.devices"),
        (WILDCARD, {"name": "web", "devices": {"host": [["type", "disk"], ["source", "/"]]}},
         "params.devices.host"),
        (WILDCARD, {"name": "web", "config": "raw.lxc=x"}, "params.config"),
    ],
    ids=["limited-config-pairs", "limited-devices-pairs", "wildcard-raw-lxc-pairs", "wildcard-devices-pairs",
         "device-entry-pairs", "config-string"],
)
async def test_update_instance_refuses_a_map_that_is_not_an_object(
    api_key: Dict[str, Any], params: Dict[str, Any], field: str
) -> None:
    outcome, client = await proxy_call("update_instance", "tenant-a", params, api_key=api_key)
    assert isinstance(outcome, HTTPException) and outcome.status_code == 400, outcome
    assert field in outcome.detail
    assert _saved(client) == [], "the instance was saved"


@pytest.mark.parametrize(
    "operation,params,field",
    [
        ("create_instance", {"name": "c1", "config": [["security.privileged", "true"]]}, "params.config"),
        ("create_instance", {"name": "c1", "devices": {"host": ["disk", "/"]}}, "params.devices.host"),
        ("create_network", {"name": "br9", "config": [["ipv4.address", "none"]]}, "params.config"),
        ("create_storage_pool", {"definition": [["name", "p1"], ["driver", "dir"]]}, "params.definition"),
        ("create_storage_pool", {"definition": {"name": "p1", "driver": "dir", "config": [["source", "/"]]}},
         "params.definition.config"),
    ],
    ids=["create-config-pairs", "create-device-entry-list", "network-config-pairs", "pool-definition-pairs",
         "pool-definition-config-pairs"],
)
async def test_every_operation_holds_its_maps_to_objects(operation: str, params: Dict[str, Any], field: str) -> None:
    outcome, client = await proxy_call(operation, "tenant-a", params, api_key=WILDCARD)
    assert isinstance(outcome, HTTPException) and outcome.status_code == 400, outcome
    assert field in outcome.detail
    assert _saved(client) == [] and not client.api.session.sent


async def test_the_refusal_comes_before_the_capability_price() -> None:
    """The dict form costs instance.exec (403). The pairs form is a malformed request (400), never a pass."""
    as_dict, _ = await proxy_call("update_instance", "tenant-a", {"name": "web", "config": {"raw.lxc": "x"}},
                                  api_key=WILDCARD)
    as_pairs, client = await proxy_call("update_instance", "tenant-a", {"name": "web", "config": [["raw.lxc", "x"]]},
                                        api_key=WILDCARD)
    assert isinstance(as_dict, HTTPException) and as_dict.status_code == 403
    assert isinstance(as_pairs, HTTPException) and as_pairs.status_code == 400
    assert _saved(client) == []


async def test_mcp_update_refuses_a_device_entry_that_is_not_an_object(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(ProjectSession())
    result = await mcp_call(
        tools_write, "lxd_update_instance", client, TWO, monkeypatch,
        cluster_id="cluster-a", name="web", project="tenant-a", devices={"host": [["type", "disk"], ["source", "/"]]},
    )
    assert result.is_error and "params.devices.host" in str(result.content)
    assert _saved(client) == []


@pytest.mark.parametrize(
    "params",
    [
        {"name": "web", "config": {"limits.cpu": "2"}, "devices": {"eth1": {"type": "nic", "network": "br0"}}},
        {"name": "web", "config": None, "devices": None, "description": "kept as dev reads it"},
    ],
    ids=["objects", "nulls-read-as-empty"],
)
async def test_objects_and_nulls_are_still_saved(params: Dict[str, Any]) -> None:
    outcome, client = await proxy_call("update_instance", "tenant-a", params, api_key=TWO)
    assert not isinstance(outcome, HTTPException) and outcome.success, outcome
    saved = _saved(client)
    assert len(saved) == 1 and saved[0]["config"] == (params["config"] or {})
