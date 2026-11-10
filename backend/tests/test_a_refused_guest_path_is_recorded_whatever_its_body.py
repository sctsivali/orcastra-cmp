"""
A refused reach inside a guest is recorded whatever its body says.

The raw proxy holds a body to one spelling (core/api_key_body.py) and prices a guest path
(core/api_key_capabilities.assert_guest_access_allowed, core/guest_data_export.py). The path gates
run first, the body rule next and the body gates after it, and every refusal of the body rule is
recorded. Otherwise `POST /exec {"Command": ["id"]}` from a key without instance.exec would answer 400
with no record, while the lowercase spelling answers 403 with a failure record. /proxy, where MCP
arrives, keeps the same record.

Every raw-proxy test drives the real handler; the session records the request it would send.
"""

from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi import HTTPException

from app.mcp import tools_write
from app.routers import integrations
from tests._limited_write_fakes import (  # noqa: F401 - fixtures are registered by import
    FakeClient,
    ProjectSession,
    _no_webhook_events,
    mcp_call,
    proxy_call,
)
from tests._raw_proxy_fakes import CLUSTER_ID, _clean_breaker, call_raw_proxy, fence_calls, key  # noqa: F401

FP = "0f3c9a1b2c3d4e5f"
NO_GUEST = key(["*"], capabilities=["inventory.read", "lxd.proxy"])
WITH_EXEC = key(["*"], capabilities=["inventory.read", "lxd.proxy", "instance.exec"])

# A guest path, an odd-cased body for it, and the capability the path costs.
GUEST_PATHS: List[Tuple[str, str, Dict[str, Any], str]] = [
    ("POST", "/1.0/instances/web-1/exec", {"Command": ["id"], "wait-for-websocket": False}, "instance.exec"),
    ("POST", "/1.0/containers/web-1/exec", {"COMMAND": ["id"]}, "instance.exec"),
    ("POST", "/1.0/instances/web-1/console", {"Width": 80}, "instance.exec"),
    ("POST", "/1.0/instances/web-1/files?path=/etc/shadow", {"Path": "/etc/shadow"}, "instance.files"),
    ("POST", "/1.0/instances/web-1/backups", {"Name": "b0"}, "instance.files"),
    ("POST", f"/1.0/images/{FP}/secret", {"Weird": True}, "instance.files"),
]
GUEST_IDS = ["exec", "containers-exec", "console", "files", "backups", "image-secret"]


@pytest.fixture
def audits(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    """What the handlers write to the audit trail, as the real _audit_key_action receives it."""
    recorded: List[Dict[str, Any]] = []
    monkeypatch.setattr(integrations, "_audit_key_action", lambda api_key, **fields: recorded.append(fields))
    return recorded


def _refusals(audits: List[Dict[str, Any]]) -> List[Tuple[str, str, Optional[str]]]:
    return [(record["result"], record["details"]["refused"], record["action"]) for record in audits]


@pytest.mark.usefixtures("fence_calls")
@pytest.mark.parametrize("method,path,body,capability", GUEST_PATHS, ids=GUEST_IDS)
async def test_an_odd_cased_guest_request_is_refused_for_its_path_and_recorded(
    method: str, path: str, body: Dict[str, Any], capability: str, audits: List[Dict[str, Any]]
) -> None:
    outcome, session = await call_raw_proxy(NO_GUEST, method, path, body=body)

    assert isinstance(outcome, HTTPException) and outcome.status_code == 403, outcome
    assert capability in outcome.detail
    assert session.calls == []
    assert [(result, refused) for result, refused, _ in _refusals(audits)] == [("failure", "capability_not_granted")]
    assert audits[0]["target_id"] == path and audits[0]["cluster_id"] == CLUSTER_ID


@pytest.mark.usefixtures("fence_calls")
async def test_the_odd_cased_exec_is_named_as_an_exec(audits: List[Dict[str, Any]]) -> None:
    outcome, _ = await call_raw_proxy(NO_GUEST, *GUEST_PATHS[0][:2], body=GUEST_PATHS[0][2])

    assert isinstance(outcome, HTTPException) and outcome.status_code == 403
    assert _refusals(audits) == [("failure", "capability_not_granted", "integration.instance.exec")]


@pytest.mark.usefixtures("fence_calls")
async def test_a_key_holding_exec_gets_the_spelling_refusal_and_its_record(audits: List[Dict[str, Any]]) -> None:
    outcome, session = await call_raw_proxy(WITH_EXEC, *GUEST_PATHS[0][:2], body=GUEST_PATHS[0][2])

    assert isinstance(outcome, HTTPException) and outcome.status_code == 400 and "'Command'" in outcome.detail
    assert session.calls == []
    assert _refusals(audits) == [("failure", "field_name_not_canonical", "integration.instance.exec")]


@pytest.mark.usefixtures("fence_calls")
@pytest.mark.parametrize(
    "method,path,body",
    [
        ("POST", "/1.0/instances/web-1", {"Migration": True}),
        ("POST", "/1.0/instances/web-1/snapshots/snap0", {"migratioN": True}),
        ("POST", "/1.0/storage-pools/default/volumes/custom/vol1", {"MIGRATION": True}),
    ],
    ids=["instance", "snapshot", "volume"],
)
async def test_an_odd_cased_migration_is_refused_and_recorded(
    method: str, path: str, body: Dict[str, Any], audits: List[Dict[str, Any]]
) -> None:
    outcome, session = await call_raw_proxy(NO_GUEST, method, path, body=body)

    assert isinstance(outcome, HTTPException) and outcome.status_code == 400, outcome
    assert session.calls == []
    assert [(result, refused) for result, refused, _ in _refusals(audits)] == [("failure", "field_name_not_canonical")]
    assert audits[0]["target_id"] == path
    assert repr(next(iter(body))) in audits[0]["error_message"]


@pytest.mark.usefixtures("fence_calls")
async def test_the_lowercase_spellings_keep_their_answers(audits: List[Dict[str, Any]]) -> None:
    exec_refused, _ = await call_raw_proxy(NO_GUEST, "POST", "/1.0/instances/web-1/exec", body={"command": ["id"]})
    migration_refused, _ = await call_raw_proxy(NO_GUEST, "POST", "/1.0/instances/web-1", body={"migration": True})
    sent, session = await call_raw_proxy(NO_GUEST, "POST", "/1.0/instances/web-1", body={"name": "web-2"})

    assert exec_refused.status_code == 403 and "instance.exec" in exec_refused.detail
    assert migration_refused.status_code == 403 and "instance.files" in migration_refused.detail
    assert sent.status_code == 200 and [verb for verb, *_ in session.calls] == ["POST"]
    assert [(record["result"], record["details"].get("refused")) for record in audits] == [
        ("failure", "capability_not_granted"), ("failure", "capability_not_granted"), ("success", None)
    ]


# -- /proxy, and MCP which calls the same handler ----------------------------------------------------


async def test_a_proxy_exec_with_odd_params_is_recorded(audits: List[Dict[str, Any]]) -> None:
    outcome, client = await proxy_call("exec_instance", "default", {"name": "web-1", "Command": ["id"]},
                                       api_key=NO_GUEST)

    assert isinstance(outcome, HTTPException) and outcome.status_code == 400, outcome
    assert client.sent == [] and client.api.session.calls == []
    assert _refusals(audits) == [("failure", "field_name_not_canonical", "integration.instance.exec")]
    assert audits[0]["target_id"] == "web-1" and audits[0]["details"]["operation"] == "exec_instance"


async def test_an_mcp_create_with_an_odd_source_is_recorded(
    monkeypatch: pytest.MonkeyPatch, audits: List[Dict[str, Any]]
) -> None:
    client = FakeClient(ProjectSession())
    result = await mcp_call(tools_write, "lxd_create_instance", client, NO_GUEST, monkeypatch,
                            cluster_id=CLUSTER_ID, name="vm", project="default",
                            source={"type": "copy", "source": "web", "Project": "tenant-c"})

    assert result.is_error is True and "'Project'" in result.content[0].text
    assert client.sent == []
    assert _refusals(audits) == [("failure", "field_name_not_canonical", "integration.proxy.request")]
