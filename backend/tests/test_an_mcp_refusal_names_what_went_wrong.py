"""A refusal over MCP names what went wrong, and the record of a tool call says how it ended.

Both dispatchers rendered every HTTPException a handler raised as `[forbidden]`. A tripped breaker
(503), a malformed name (400), an instance that does not exist (404) and an ambiguous one (409)
all told a model "this key may not do that", which sends it to ask an operator for a grant it
already holds rather than wait, fix its arguments or give up. The code now follows the status the
REST route would have answered, in one vocabulary: PROXY_ERROR_CODES plus `forbidden`.

The audit row for a tool call was written before the call with the logger's default result, so a
refused delete was recorded as a successful one.
"""

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

import app.routers.integrations as integrations
from app.core.blocking import BlockingPoolFull, BlockingTimeout
from app.mcp import dispatch as mcp_dispatch
from app.mcp import tools_admin, tools_inventory, tools_read, tools_write
from app.mcp.errors import _STATUS_CODES, error_code_for, refusal_result
from app.services.lxd import health
from app.services.lxd.proxy_operations import PROXY_ERROR_CODES
from tests._limited_write_fakes import (
    CLUSTER_ID,
    FakeClient,
    ProjectSession,
    ToolRegistry,
    fake_service,
    mcp_call,
)
from tests._raw_proxy_fakes import key


def _text(result: Any) -> str:
    return next(c.text for c in result.content if getattr(c, "type", None) == "text")


def _code(result: Any) -> str:
    text = _text(result)
    assert text.startswith("[") and "]" in text, f"no error code on: {text[:120]}"
    return text[1:text.index("]")]


# ── the table ──────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "status,expected",
    [
        (400, "invalid_request"), (401, "forbidden"), (403, "forbidden"), (404, "not_found"),
        (409, "conflict"), (422, "invalid_request"), (429, "overloaded"), (502, "operation_failed"),
        (503, "cluster_unavailable"), (504, "timeout"), (418, "invalid_request"),
        (500, "operation_failed"),
    ],
)
def test_the_code_follows_the_status(status: int, expected: str) -> None:
    assert error_code_for(HTTPException(status_code=status, detail="x")) == expected


def test_a_full_pool_is_a_busy_server_not_a_down_cluster() -> None:
    try:
        try:
            raise BlockingPoolFull("op", 32)
        except BlockingPoolFull as pool_error:
            raise HTTPException(status_code=503, detail=str(pool_error)) from pool_error
    except HTTPException as exc:
        assert error_code_for(exc) == "overloaded"


def test_every_code_is_one_a_caller_already_knows() -> None:
    """One vocabulary whichever check answered: the proxy's own codes, plus forbidden."""
    known = set(PROXY_ERROR_CODES) | {"forbidden"}
    produced = set(_STATUS_CODES.values()) | {"invalid_request", "operation_failed", "overloaded"}
    assert produced <= known, f"codes outside the vocabulary: {sorted(produced - known)}"


def test_a_retry_after_reaches_the_model() -> None:
    """A tool result has no headers, and 'overloaded' without a wait invites an immediate retry."""
    exc = HTTPException(status_code=429, detail="desktop budget spent.", headers={"Retry-After": "17"})
    result = refusal_result(exc)
    assert result.is_error
    assert _text(result) == "[overloaded] desktop budget spent. Retry after 17s."


# ── both dispatchers, through the tools a model calls ──────────────────────────────────────────

@pytest.mark.parametrize(
    "status,expected",
    [(403, "forbidden"), (400, "invalid_request"), (503, "cluster_unavailable"), (409, "conflict")],
)
def test_call_proxy_names_the_status(monkeypatch, status: int, expected: str) -> None:
    async def _refuse(**_: Any) -> Any:
        raise HTTPException(status_code=status, detail="refused")

    monkeypatch.setattr(integrations, "proxy_operation", _refuse)
    result = asyncio.run(mcp_call(
        tools_read, "lxd_list_instances", FakeClient(ProjectSession()), key(["*"], ["lxd.proxy"]),
        monkeypatch, cluster_id=CLUSTER_ID,
    ))
    assert result.is_error and _code(result) == expected


@pytest.mark.parametrize("status,expected", [(404, "not_found"), (409, "conflict"), (503, "cluster_unavailable")])
def test_run_inventory_names_the_status(monkeypatch, status: int, expected: str) -> None:
    async def _refuse(**_: Any) -> Any:
        raise HTTPException(status_code=status, detail="no such instance")

    monkeypatch.setattr(integrations, "get_integration_instance", _refuse)
    result = asyncio.run(mcp_call(
        tools_inventory, "orcastra_get_instance", FakeClient(ProjectSession()),
        key(["*"], ["inventory.read"]), monkeypatch, name="web",
    ))
    assert result.is_error and _code(result) == expected


def test_a_tripped_breaker_reads_as_unavailable_not_forbidden(monkeypatch) -> None:
    """The real proxy_operation, a cluster the breaker has tripped: wait, do not ask for a grant."""
    health.record_failure(CLUSTER_ID, "connection refused")
    try:
        assert health.is_tripped(CLUSTER_ID)
        result = asyncio.run(mcp_call(
            tools_read, "lxd_list_instances", FakeClient(ProjectSession()), key(["*"], ["lxd.proxy"]),
            monkeypatch, cluster_id=CLUSTER_ID,
        ))
    finally:
        health.clear(CLUSTER_ID)
    assert result.is_error and _code(result) == "cluster_unavailable", _text(result)


def test_a_dot_name_reads_as_a_bad_request(monkeypatch) -> None:
    result = asyncio.run(mcp_call(
        tools_read, "lxd_get_instance", FakeClient(ProjectSession()), key(["*"], ["lxd.proxy"]),
        monkeypatch, cluster_id=CLUSTER_ID, name="..",
    ))
    assert result.is_error and _code(result) == "invalid_request", _text(result)


def test_an_ungranted_project_is_still_forbidden(monkeypatch) -> None:
    result = asyncio.run(mcp_call(
        tools_read, "lxd_list_instances", FakeClient(ProjectSession()),
        key(["tenant-a"], ["lxd.proxy"]), monkeypatch, cluster_id=CLUSTER_ID, project="tenant-b",
    ))
    assert result.is_error and _code(result) == "forbidden"


@pytest.mark.parametrize(
    "raised,expected",
    [(BlockingPoolFull("op", 32), "overloaded"), (BlockingTimeout("op", 20.0), "timeout")],
)
def test_an_operation_poll_tells_busy_from_slow(monkeypatch, raised: Exception, expected: str) -> None:
    async def _blocked(*_: Any, **__: Any) -> Any:
        raise raised

    monkeypatch.setattr(integrations, "run_blocking", _blocked)
    result = asyncio.run(mcp_call(
        tools_inventory, "orcastra_get_operation", FakeClient(ProjectSession()),
        key(["*"], ["inventory.read"]), monkeypatch, operation_id="op-1", cluster_id=CLUSTER_ID,
    ))
    assert result.is_error and _code(result) == expected


# ── whoami ─────────────────────────────────────────────────────────────────────────────────────

def test_whoami_answers_a_key_without_inventory_read_in_json(monkeypatch) -> None:
    """The tool the instructions say to call first, for a key that holds lxd.proxy alone."""
    monkeypatch.setattr(integrations, "_integration_features", lambda: [])
    result = asyncio.run(mcp_call(
        tools_inventory, "orcastra_whoami", FakeClient(ProjectSession()), key(["*"], ["lxd.proxy"]),
        monkeypatch,
    ))
    assert not result.is_error, _text(result)
    parsed = json.loads(_text(result))
    assert parsed["valid"] is True
    assert parsed["capabilities"] == ["lxd.proxy"]
    assert result.structured_content["capabilities"] == ["lxd.proxy"]


# ── the two read fixes ─────────────────────────────────────────────────────────────────────────

def _capture_proxy(monkeypatch) -> List[Any]:
    seen: List[Any] = []

    async def _proxy(request: Any, **_: Any) -> Any:
        seen.append(request)
        return SimpleNamespace(success=True, data=[], error=None, error_code=None)

    monkeypatch.setattr(integrations, "proxy_operation", _proxy)
    return seen


def test_list_projects_names_a_project_the_key_was_granted(monkeypatch) -> None:
    """`default` was sent whatever the grant, so a key limited to other projects was refused."""
    seen = _capture_proxy(monkeypatch)
    result = asyncio.run(mcp_call(
        tools_read, "lxd_list_projects", FakeClient(ProjectSession()),
        key(["tenant-b", "tenant-a"], ["lxd.proxy"]), monkeypatch, cluster_id=CLUSTER_ID,
    ))
    assert not result.is_error
    assert seen[0].project == "tenant-a"


@pytest.mark.parametrize("projects", [["*"], ["default", "tenant-a"]])
def test_list_projects_keeps_default_where_it_is_granted(monkeypatch, projects: List[str]) -> None:
    seen = _capture_proxy(monkeypatch)
    asyncio.run(mcp_call(
        tools_read, "lxd_list_projects", FakeClient(ProjectSession()), key(projects, ["lxd.proxy"]),
        monkeypatch, cluster_id=CLUSTER_ID,
    ))
    assert seen[0].project == "default"


def test_lxd_get_operation_polls_through_the_proxy(monkeypatch) -> None:
    seen = _capture_proxy(monkeypatch)
    result = asyncio.run(mcp_call(
        tools_read, "lxd_get_operation", FakeClient(ProjectSession()), key(["*"], ["lxd.proxy"]),
        monkeypatch, cluster_id=CLUSTER_ID, operation_id="op-7",
    ))
    assert not result.is_error
    assert (seen[0].operation, seen[0].params) == ("get_operation", {"operation_id": "op-7"})


@pytest.mark.parametrize("projects, expected", [
    (["tenant-b", "tenant-a"], "tenant-a"), (["*"], "default"), (["default", "tenant-a"], "default"),
])
def test_lxd_get_operation_without_a_project_names_one_the_key_holds(monkeypatch, projects, expected) -> None:
    """`default` was sent whatever the grant, so a key limited to other projects could not poll
    the operations it started without passing `project`. Seen live on nuc2, 2026-10-14."""
    seen = _capture_proxy(monkeypatch)
    result = asyncio.run(mcp_call(
        tools_read, "lxd_get_operation", FakeClient(ProjectSession()), key(projects, ["lxd.proxy"]),
        monkeypatch, cluster_id=CLUSTER_ID, operation_id="op-7",
    ))
    assert not result.is_error
    assert seen[0].project == expected


def test_lxd_get_operation_keeps_a_project_the_caller_named(monkeypatch) -> None:
    seen = _capture_proxy(monkeypatch)
    asyncio.run(mcp_call(
        tools_read, "lxd_get_operation", FakeClient(ProjectSession()),
        key(["tenant-b", "tenant-a"], ["lxd.proxy"]), monkeypatch,
        cluster_id=CLUSTER_ID, operation_id="op-7", project="tenant-b",
    ))
    assert seen[0].project == "tenant-b"


@pytest.mark.parametrize("named, expected", [(None, "tenant-a"), ("tenant-b", "tenant-b")])
def test_orcastra_get_operation_names_a_project_the_key_holds(monkeypatch, named, expected) -> None:
    seen: List[Dict[str, Any]] = []

    async def _operation(**kwargs: Any) -> Dict[str, Any]:
        seen.append(kwargs)
        return {"id": kwargs["operation_id"], "status": "Running"}

    monkeypatch.setattr(integrations, "get_integration_operation", _operation)
    arguments: Dict[str, Any] = {"cluster_id": CLUSTER_ID, "operation_id": "op-7"}
    if named:
        arguments["project"] = named
    result = asyncio.run(mcp_call(
        tools_inventory, "orcastra_get_operation", FakeClient(ProjectSession()),
        key(["tenant-b", "tenant-a"], ["inventory.read"]), monkeypatch, **arguments,
    ))
    assert not result.is_error
    assert seen[0]["project"] == expected


# ── the audit row ──────────────────────────────────────────────────────────────────────────────

class _Recorder:
    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []

    def log_activity(self, **kwargs: Any) -> str:
        self.rows.append(kwargs)
        return "id"


def _recorded(monkeypatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(mcp_dispatch, "get_activity_logger", lambda: recorder)
    return recorder


def test_a_refused_call_is_recorded_as_denied(monkeypatch) -> None:
    recorder = _recorded(monkeypatch)
    # A key holding read only: the tool is called anyway and the proxy refuses the level.
    read_only = {
        "id": "oak_read", "app_name": "qa", "capabilities": ["lxd.proxy"],
        "cluster_permissions": [{"cluster_id": CLUSTER_ID, "permissions": ["read"], "projects": ["*"]}],
    }
    result = asyncio.run(mcp_call(
        tools_admin, "lxd_delete_instance", FakeClient(ProjectSession()), read_only, monkeypatch,
        cluster_id=CLUSTER_ID, name="web",
    ))
    assert result.is_error
    (row,) = recorder.rows
    assert row["action"] == "integration.mcp.tool_call"
    assert (row["result"], row["error_code"]) == ("denied", "forbidden")
    assert row["details"]["name"] == "web"
    assert row["details"]["operation"] == "delete_instance"


def test_a_failed_call_is_recorded_as_a_failure(monkeypatch) -> None:
    recorder = _recorded(monkeypatch)
    health.record_failure(CLUSTER_ID, "connection refused")
    try:
        asyncio.run(mcp_call(
            tools_write, "lxd_start_instance", FakeClient(ProjectSession()), key(["*"], ["lxd.proxy"]),
            monkeypatch, cluster_id=CLUSTER_ID, name="web",
        ))
    finally:
        health.clear(CLUSTER_ID)
    (row,) = recorder.rows
    assert (row["result"], row["error_code"]) == ("failure", "cluster_unavailable")


def test_a_successful_call_is_recorded_without_the_values_it_carried(monkeypatch) -> None:
    recorder = _recorded(monkeypatch)
    _capture_proxy(monkeypatch)
    asyncio.run(mcp_call(
        tools_write, "lxd_update_instance", FakeClient(ProjectSession()), key(["*"], ["lxd.proxy"]),
        monkeypatch, cluster_id=CLUSTER_ID, name="web", config={"user.secret-note": "hunter2"},
    ))
    (row,) = recorder.rows
    assert (row["result"], row["error_code"]) == ("success", None)
    assert row["details"]["param_names"] == ["config", "name"]
    assert "hunter2" not in json.dumps(row)


def test_a_call_that_raises_is_still_recorded(monkeypatch) -> None:
    recorder = _recorded(monkeypatch)

    async def _boom(**_: Any) -> Any:
        raise RuntimeError("handler bug")

    monkeypatch.setattr(integrations, "proxy_operation", _boom)
    with pytest.raises(RuntimeError):
        asyncio.run(mcp_call(
            tools_read, "lxd_list_instances", FakeClient(ProjectSession()), key(["*"], ["lxd.proxy"]),
            monkeypatch, cluster_id=CLUSTER_ID,
        ))
    (row,) = recorder.rows
    assert (row["result"], row["error_code"]) == ("failure", "outcome_unknown")


# ── a desktop wait never takes a pool worker ───────────────────────────────────────────────────

class _NoSession:
    async def __aenter__(self) -> "_NoSession":
        raise RuntimeError("no database in this test")

    async def __aexit__(self, *_: Any) -> None:
        return None


def test_a_desktop_wait_is_served_without_the_pool(monkeypatch) -> None:
    """Four agents waiting the maximum used to refuse every other desktop action for its length."""
    from app.core import database
    from app.routers.integrations import ProxyRequest, proxy_operation
    from app.services.desktop import entry

    async def _allowed(*_: Any, **__: Any) -> None:
        return None

    async def _never(*_: Any, **__: Any) -> Any:
        raise AssertionError("a wait was handed to a worker pool")

    async def _no_event(*_: Any, **__: Any) -> None:
        return None

    audited: List[Dict[str, Any]] = []
    monkeypatch.setattr(integrations, "_assert_desktop_allowed", _allowed)
    monkeypatch.setattr(integrations, "run_blocking", _never)
    monkeypatch.setattr(integrations, "_audit_key_action", lambda *a, **k: audited.append(k))
    monkeypatch.setattr(entry, "DESKTOP_CONTROL_ENABLED", True)
    monkeypatch.setattr(database, "async_session_maker", lambda: _NoSession())
    from app.services.webhooks import emitter

    monkeypatch.setattr(emitter, "emit_for_proxy_operation", _no_event)

    desktop_key = key(["*"], ["lxd.proxy", "desktop.input"])

    async def _four_waits() -> List[Any]:
        return await asyncio.gather(*(
            proxy_operation(
                request=ProxyRequest(
                    cluster_id=CLUSTER_ID, operation="desktop_input", project="default",
                    params={"name": "desk", "action": "wait", "duration": 0.05},
                ),
                api_key=desktop_key, lxd_service=fake_service(FakeClient(ProjectSession())), _=None,
            )
            for _ in range(4)
        ))

    answers = asyncio.run(_four_waits())
    assert all(a.success and a.data["action"] == "wait" for a in answers)
    assert all(a.data["waited_seconds"] == pytest.approx(0.05) for a in answers)
    assert len(audited) == 4, "a wait must still leave the audit record every desktop action leaves"


def test_waits_beyond_a_workers_share_are_overloaded(monkeypatch) -> None:
    """The pool used to bound waits; on the event loop the per-worker share does, so a caller
    cannot hold an HTTP slot per made-up instance name."""
    from app.services.desktop import entry

    monkeypatch.setattr(entry, "_WAIT_SLOTS", asyncio.Semaphore(2))

    async def _three() -> List[Any]:
        return await asyncio.gather(*(entry.serve_wait(0.05) for _ in range(3)), return_exceptions=True)

    answers = asyncio.run(_three())
    assert sum(isinstance(a, dict) and a["ok"] for a in answers) == 2
    assert sum(isinstance(a, BlockingPoolFull) for a in answers) == 1


def test_a_wait_is_validated_as_the_guest_path_validates_it(monkeypatch) -> None:
    from app.services.desktop import entry

    monkeypatch.setattr(entry, "DESKTOP_CONTROL_ENABLED", True)
    assert entry.wait_seconds("desktop_view", {"action": "screenshot"}) is None
    assert entry.wait_seconds("desktop_probe", {"action": "wait"}) is None
    assert entry.wait_seconds("desktop_clipboard", {"action": "wait", "duration": 0.5}) == 0.5
    monkeypatch.setattr(entry, "DESKTOP_CONTROL_ENABLED", False)
    with pytest.raises(HTTPException) as refused:
        entry.wait_seconds("desktop_input", {"action": "wait", "duration": 1})
    assert refused.value.status_code == 403


def test_every_registered_tool_module_keeps_a_registry_shape() -> None:
    """ToolRegistry stands in for the server here; a module that changed its register signature
    would make every test above pass vacuously."""
    for module in (tools_inventory, tools_read, tools_write, tools_admin):
        registry = ToolRegistry()
        module.register(registry)
        assert registry.tools, module.__name__


_VAULT_TEXT = "Failed to read https://vault.internal:8200/v1/secret/data/lxd/clusters/c1/certs"


@pytest.mark.parametrize("status", [502, 503, 504])
def test_a_server_refusal_reaches_the_model_without_its_url(status: int) -> None:
    """A breaker reason is raw exception text. REST strips it; MCP sent it verbatim."""
    text = _text(refusal_result(HTTPException(status_code=status, detail=f"Cluster c1 down: {_VAULT_TEXT}")))
    assert "Cluster c1 down" in text
    assert "vault.internal" not in text and "/v1/secret" not in text


def test_an_unexpected_fault_reaches_the_model_as_a_reference() -> None:
    text = _text(refusal_result(HTTPException(status_code=500, detail=_VAULT_TEXT)))
    assert "Reference:" in text and "vault" not in text.lower()


def test_a_structured_server_refusal_keeps_its_message() -> None:
    detail = {"error": "identity_provider_unavailable", "message": "Retry shortly."}
    refused = HTTPException(status_code=503, detail=detail, headers={"Retry-After": "30"})
    assert _text(refusal_result(refused)) == f"[{error_code_for(refused)}] Retry shortly. Retry after 30s."
