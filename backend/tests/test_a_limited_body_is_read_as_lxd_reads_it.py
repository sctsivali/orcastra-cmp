"""An API key's request body is read the way LXD reads it: a field spelled otherwise is refused.

LXD decodes a request body with Go's encoding/json, which matches a struct field whatever its case,
and with Unicode case folding, so "Profiles", "CONFIG" or a long-s "source" are the fields
profiles, config and source to LXD. Every body rule here (the project named in the body,
host-reaching settings, the profiles a write applies, the migration fence's stop exemption, the
policy-key observer, the guest-capability price of a /proxy create) reads the plain lowercase
spelling, so a body that spells a field another way would carry past all of them. Map keys (config
keys, device names) match exactly and are left alone.

Ported from the RC4-hotfix2 line, where the rule held a project-limited key only. On this line it
holds every key (owner decision 3, 2026-09-25), so the `*` case is inverted: a `*` key is refused
too, and its lowercase body still goes through.
"""

from typing import Any, List

import pytest
from fastapi import HTTPException

from app.core.api_key_body import assert_canonical_proxy_params, canonical_raw_proxy_body, odd_field_name
from app.mcp import tools_write
from tests._limited_write_fakes import (  # noqa: F401 - fixtures are registered by import
    NARROW,
    TWO,
    WILDCARD,
    FakeClient,
    ProjectSession,
    _no_webhook_events,
    mcp_call,
    proxy_call,
    raw_write,
)
from tests._raw_proxy_fakes import _clean_breaker, fence_calls, key  # noqa: F401

LONG_S = chr(0x17F) + "ource"  # the long s, which Go folds to s
KELVIN_KIND = chr(0x212A) + "ind"  # the Kelvin sign, which Go folds to k

ODD_BODIES = [
    ("POST", "/1.0/instances", {"name": "c1", "Profiles": ["priv"]}, "Profiles"),
    ("POST", "/1.0/instances", {"name": "c1", "profiles": ["default"], "CONFIG": {"security.privileged": "true"}},
     "CONFIG"),
    ("POST", "/1.0/instances", {"name": "c1", "Devices": {"d": {"type": "unix-block", "source": "/dev/sda"}}},
     "Devices"),
    ("POST", "/1.0/instances/web", {"Project": "tenant-c"}, "Project"),
    ("PUT", "/1.0/instances/app", {"Restore": "s0"}, "Restore"),
    ("POST", "/1.0/instances", {"name": "c1", LONG_S: {"type": "copy", "source": "web"}}, LONG_S),
    ("POST", "/1.0/instances", {"name": "c1", "source": {"type": "copy", "source": "web", "Project": "tenant-c"}},
     "Project"),
    ("POST", "/1.0/instances/web", {"name": "web2", "target": {"Certificate": "x"}}, "Certificate"),
    ("POST", "/1.0/images", {"source": {"type": "instance", "name": "web", KELVIN_KIND: "x"}}, KELVIN_KIND),
    ("PUT", "/1.0/instances/web/state", {"action": "stop", "Action": "restart"}, "Action"),
    ("POST", "/1.0/instances", {"name": "c1", "Name": "c2"}, "Name"),
]
IDS = ["Profiles", "CONFIG", "Devices", "Project", "Restore", "long-s", "nested-Project", "target-struct",
       "kelvin-in-source", "twin-Action", "twin-Name"]


@pytest.mark.parametrize("method,path,body,field", ODD_BODIES, ids=IDS)
@pytest.mark.parametrize("api_key", [TWO, WILDCARD], ids=["limited", "star"])
async def test_a_body_field_spelled_otherwise_is_refused_for_every_key(method, path, body, field, api_key) -> None:
    outcome, session = await raw_write(method, path, "tenant-a", body, api_key=api_key)
    assert isinstance(outcome, HTTPException) and outcome.status_code == 400
    assert repr(field) in outcome.detail, "the refusal must name the field"
    assert session.calls == [], "a refused body reached the cluster"


async def test_map_keys_are_left_to_lxd() -> None:
    """Config keys and device names are map keys, which LXD matches exactly, so any spelling passes."""
    body = {"name": "c1", "profiles": ["default"], "config": {"User.Note": "x"},
            "devices": {"Eth0": {"type": "nic", "network": "lxdbr0"}}}
    outcome, session = await raw_write("POST", "/1.0/instances", "tenant-a", body, api_key=WILDCARD)
    assert outcome.status_code == 200 and session.sent


async def test_a_star_key_lowercase_body_still_goes_through() -> None:
    outcome, session = await raw_write("POST", "/1.0/instances", "tenant-a",
                                       {"name": "c1", "profiles": ["priv"], "source": {"type": "none"}},
                                       api_key=WILDCARD)
    assert outcome.status_code == 200
    assert [method for method, *_ in session.calls] == ["POST"], "a star key reads nothing before its write"


# -- every guard reads the dict that is forwarded ------------------------------------------------


async def test_a_twin_action_can_no_longer_take_the_fence_stop_exemption(fence_calls) -> None:
    """{"action": "stop", "Action": "restart"} was judged a stop and reached LXD as a restart."""
    outcome, session = await raw_write("PUT", "/1.0/instances/web/state", "tenant-a",
                                       {"action": "stop", "Action": "restart"}, api_key=WILDCARD)
    assert isinstance(outcome, HTTPException) and outcome.status_code == 400
    assert session.calls == [] and fence_calls == []


async def test_a_plain_stop_keeps_its_exemption_and_a_restart_is_fenced(fence_calls) -> None:
    await raw_write("PUT", "/1.0/instances/web/state", "tenant-a", {"action": "stop"}, api_key=WILDCARD)
    assert [name for name, _ in fence_calls] == []
    await raw_write("PUT", "/1.0/instances/web/state", "tenant-a", {"action": "restart"}, api_key=WILDCARD)
    assert [(name, kwargs["instance_name"]) for name, kwargs in fence_calls] == [
        ("_assert_proxy_write_is_not_fenced", "web")]


async def test_a_body_lxd_never_receives_takes_no_exemption(fence_calls) -> None:
    """A DELETE sends no body, so a body riding along cannot make it a stop."""
    await raw_write("DELETE", "/1.0/instances/web/state", "tenant-a", {"action": "stop"}, api_key=WILDCARD)
    assert [(name, kwargs["instance_name"]) for name, kwargs in fence_calls] == [
        ("_assert_proxy_write_is_not_fenced", "web")]


async def test_a_twin_name_cannot_steer_the_fence_to_another_instance(fence_calls) -> None:
    outcome, session = await raw_write("POST", "/1.0/instances", "tenant-a", {"name": "free", "Name": "fenced"},
                                       api_key=WILDCARD)
    assert isinstance(outcome, HTTPException) and outcome.status_code == 400
    assert fence_calls == [] and session.calls == []


async def test_a_policy_key_spelled_otherwise_is_refused_not_missed(monkeypatch) -> None:
    from app.routers import integrations

    observed: List[Any] = []
    monkeypatch.setattr(integrations, "_audit_key_action", lambda *a, **k: observed.append(k.get("action")))
    outcome, session = await raw_write("PATCH", "/1.0/instances/web", "tenant-a",
                                       {"Config": {"snapshots.schedule": "@daily"}}, api_key=WILDCARD)
    assert isinstance(outcome, HTTPException) and outcome.status_code == 400 and not session.sent
    outcome, session = await raw_write("PATCH", "/1.0/instances/web", "tenant-a",
                                       {"config": {"snapshots.schedule": "@daily"}}, api_key=WILDCARD)
    assert outcome.status_code == 200 and "integration.instance.config.policy_key_write" in observed


def test_the_body_forwarded_is_the_body_judged() -> None:
    body = {"name": "c1", "config": {}}
    assert canonical_raw_proxy_body("POST", body) is body
    assert canonical_raw_proxy_body("GET", body) is None
    assert canonical_raw_proxy_body("DELETE", {"Action": "stop"}) is None
    assert odd_field_name({"source": "a-string", "target": 3}) is None
    assert odd_field_name({"a b": 1}) == "a b"
    assert odd_field_name({"name\n": 1}) == "name\n", "a trailing newline must not pass the pattern"


def test_each_alias_entry_is_a_struct_and_nothing_below_it_is() -> None:
    """LXD decodes an image create's aliases into api.ImageAlias structs, so each entry's field names fold."""
    assert odd_field_name({"aliases": [{"name": "ok", "NAME": ".."}]}) == "NAME"
    assert odd_field_name({"aliases": [{"name": "a"}, {"Name": ".."}]}) == "Name"
    assert odd_field_name({"aliases": [{"name": "ok", "description": "Up"}], "properties": {"OS": "x"}}) is None
    assert odd_field_name({"aliases": [{"name": "ok", "extra": {"Nested": 1}}]}) is None
    assert odd_field_name({"aliases": "mkt/acme/app"}) is None
    assert odd_field_name({"aliases": [None, "a", 1, ["NAME"]]}) is None
    assert odd_field_name({"source": {"aliases": [{"NAME": ".."}]}}) is None


# -- /proxy params and the body each operation forwards -------------------------------------------


@pytest.mark.parametrize("api_key", [TWO, WILDCARD], ids=["limited", "star"])
async def test_proxy_create_refuses_an_odd_field_spelling(api_key) -> None:
    outcome, client = await proxy_call("create_instance", "tenant-b", {"name": "vm", "Profiles": ["priv"]},
                                       api_key=api_key)
    assert isinstance(outcome, HTTPException) and outcome.status_code == 400 and "'Profiles'" in outcome.detail
    assert client.sent == [] and client.api.session.calls == []


async def test_proxy_create_no_longer_prices_a_config_it_cannot_read() -> None:
    """create_instance forwards params whole, so `Config` reached LXD as config without instance.exec."""
    star_without_exec = key(["*"], capabilities=["lxd.proxy"])
    outcome, client = await proxy_call("create_instance", "default",
                                       {"name": "vm", "Config": {"raw.lxc": "lxc.hook.pre-start=/bin/sh"}},
                                       api_key=star_without_exec)
    assert isinstance(outcome, HTTPException) and outcome.status_code == 400 and client.sent == []
    outcome, client = await proxy_call("create_instance", "default",
                                       {"name": "vm", "config": {"raw.lxc": "lxc.hook.pre-start=/bin/sh"}},
                                       api_key=star_without_exec)
    assert isinstance(outcome, HTTPException) and outcome.status_code == 403 and "instance.exec" in outcome.detail
    assert client.sent == []


@pytest.mark.parametrize(
    "operation,params,field",
    [
        ("create_storage_pool", {"definition": {"name": "p1", "driver": "dir", "Config": {"source": "/"}}}, "Config"),
        ("create_storage_pool", {"name": "p1", "driver": "dir", "CONFIG": {"source": "/"}}, "CONFIG"),
        ("update_instance", {"name": "web", "Devices": {"d": {"type": "unix-block"}}}, "Devices"),
        ("start_instance", {"name": "web", "Force": True}, "Force"),
    ],
    ids=["pool-definition", "pool-params", "update", "start"],
)
async def test_proxy_params_and_the_forwarded_definition_are_held_to_the_spelling(operation, params, field) -> None:
    outcome, client = await proxy_call(operation, "default", params, api_key=WILDCARD)
    assert isinstance(outcome, HTTPException) and outcome.status_code == 400 and repr(field) in outcome.detail
    assert client.sent == []


def test_only_the_operation_that_forwards_a_definition_has_it_read() -> None:
    assert_canonical_proxy_params("create_network", {"name": "n", "definition": {"Weird": 1}})
    with pytest.raises(HTTPException):
        assert_canonical_proxy_params("create_storage_pool", {"name": "n", "definition": {"Weird": 1}})


# -- MCP builds /proxy params, and a model chooses the nested source ----------------------------------


async def test_mcp_create_refuses_an_odd_spelling_inside_source(monkeypatch) -> None:
    client = FakeClient(ProjectSession())
    result = await mcp_call(tools_write, "lxd_create_instance", client, WILDCARD, monkeypatch,
                            cluster_id="cluster-a", name="vm", project="tenant-a",
                            source={"type": "copy", "source": "web", "Project": "tenant-c"})
    assert result.is_error is True and "'Project'" in result.content[0].text
    assert client.sent == [] and client.api.session.calls == []


async def test_mcp_create_with_a_lowercase_source_goes_through(monkeypatch) -> None:
    client = FakeClient(ProjectSession())
    result = await mcp_call(tools_write, "lxd_create_instance", client, WILDCARD, monkeypatch,
                            cluster_id="cluster-a", name="vm", project="tenant-a", source={"type": "none"})
    assert result.is_error is False, result.content
    assert [verb for verb, *_ in client.sent] == ["create"]
