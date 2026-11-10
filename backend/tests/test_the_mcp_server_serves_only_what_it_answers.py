"""The MCP transport serves the methods this server answers, at the sizes its tools promise.

SDK 2.2 registers `subscriptions/listen` on every server. On the `MODERN` protocol revision, which
the SDK selects by the MCP-Protocol-Version header, it is served as an SSE stream pinging every 15 s
for as long as the client stays connected, `json_response` notwithstanding. Served, it would let
any authenticated key, even one holding no capability, hold connections open until a worker's
`--limit-concurrency` shed everything, /health included.

The SDK's default request cap is 4 MiB, while `lxd_write_file` promises FILE_WRITE_LIMIT decoded,
whose base64 form is a third larger.
"""

import asyncio
import base64
import math
from typing import Any, Dict, List, Optional

import httpx
import pytest
from fastapi import HTTPException
from mcp.server.transport_security import DEFAULT_MAX_REQUEST_BODY_SIZE
from starlette.testclient import TestClient

import app.routers.integrations as integrations
from app.mcp.limits import request_body_limit
from app.services.lxd import proxy as lxd_proxy
from tests.test_the_mcp_server_authenticates_every_call import (  # noqa: F401 - fixtures
    CREDENTIAL,
    HEADERS,
    _authenticated_as,
    _initialize,
    _key,
    _rpc,
    _tool_names,
    mcp_app,
    mcp_app_full,
)

MODERN = "2026-07-28"
META = {
    "io.modelcontextprotocol/protocolVersion": MODERN,
    "io.modelcontextprotocol/clientCapabilities": {},
}


def _modern(client: TestClient, method: str, params: Optional[Dict[str, Any]] = None, **kw: Any):
    headers = {**HEADERS, **CREDENTIAL, "MCP-Protocol-Version": MODERN, "Mcp-Method": method}
    if method == "tools/call":
        headers["Mcp-Name"] = (params or {})["name"]
    return client.post(
        "/api/v1/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": {**(params or {}), "_meta": META}},
        **kw,
    )


def _counting(monkeypatch, api_key: Dict[str, Any]) -> List[int]:
    calls: List[int] = []

    async def _validate(x_api_key_id=None, x_api_key_secret=None, **_):
        calls.append(1)
        return api_key

    monkeypatch.setattr(integrations, "validate_api_key", _validate)
    return calls


# ── the change stream nothing publishes to ─────────────────────────────────────────────────────

def test_the_sdk_listen_handler_is_not_registered() -> None:
    """Fails if an SDK release moves the handler table, which is when this needs looking at."""
    from app.core.config import get_settings
    from app.mcp.server import build_mcp_server

    server = build_mcp_server(get_settings())
    assert server._lowlevel_server.get_request_handler("subscriptions/listen") is None
    assert server._lowlevel_server.get_request_handler("tools/call") is not None


def test_discover_advertises_no_change_stream(mcp_app, monkeypatch) -> None:
    _authenticated_as(monkeypatch, _key(["inventory.read"]))
    with TestClient(mcp_app) as client:
        response = _modern(client, "server/discover")
    assert response.status_code == 200, response.text
    capabilities = response.json()["result"]["capabilities"]
    assert not (capabilities.get("tools") or {}).get("listChanged")
    assert not (capabilities.get("resources") or {}).get("subscribe")


def test_a_listen_request_is_refused_at_once(mcp_app, monkeypatch) -> None:
    """Bounded by a timeout so the defect fails this test instead of hanging the suite."""
    _authenticated_as(monkeypatch, _key([], clusters=[]))
    manager = mcp_app.state.mcp_session_manager

    async def _listen() -> httpx.Response:
        async with manager.run():
            transport = httpx.ASGITransport(app=mcp_app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.post(
                    "/api/v1/mcp",
                    headers={**HEADERS, **CREDENTIAL, "MCP-Protocol-Version": MODERN,
                             "Mcp-Method": "subscriptions/listen"},
                    json={"jsonrpc": "2.0", "id": 9, "method": "subscriptions/listen",
                          "params": {"_meta": META, "notifications": {"toolsListChanged": True}}},
                )

    async def _bounded() -> httpx.Response:
        return await asyncio.wait_for(_listen(), timeout=10)

    response = asyncio.run(_bounded())
    assert "event-stream" not in response.headers.get("content-type", "")
    assert response.json()["error"]["code"] == -32601


def test_a_method_outside_the_served_set_is_refused_on_the_handshake_wire(mcp_app, monkeypatch) -> None:
    """`resources/read` is registered by the SDK; this server serves no resource to read."""
    _authenticated_as(monkeypatch, _key(["inventory.read"]))
    with TestClient(mcp_app) as client:
        _initialize(client)
        response = _rpc(client, "resources/read", {"uri": "file:///etc/passwd"})
    error = response.json()["error"]
    assert error["code"] == -32601 and "not served" in error["message"]


@pytest.mark.parametrize("method", ["ping", "tools/list", "resources/list", "prompts/list"])
def test_the_methods_a_client_asks_for_are_still_answered(mcp_app, monkeypatch, method: str) -> None:
    _authenticated_as(monkeypatch, _key(["inventory.read"]))
    with TestClient(mcp_app) as client:
        _initialize(client)
        response = _rpc(client, method)
    assert response.status_code == 200 and "result" in response.json(), response.text


# ── the modern wire authenticates and narrows like the handshake one ───────────────────────────

def test_the_modern_wire_narrows_tools_list(mcp_app, monkeypatch) -> None:
    _authenticated_as(monkeypatch, _key(["inventory.read"]))
    with TestClient(mcp_app) as client:
        response = _modern(client, "tools/list")
    names = sorted(tool["name"] for tool in response.json()["result"]["tools"])
    assert names and all(name.startswith("orcastra_") for name in names), names


def test_the_modern_wire_refuses_on_the_tool_channel_and_validates_once(mcp_app, monkeypatch) -> None:
    """The SDK lists the tools internally before every tools/call that carries arguments; that
    listing must not spend the key's budget a second time."""
    calls = _counting(monkeypatch, _key(["inventory.read"]))
    with TestClient(mcp_app) as client:
        response = _modern(client, "tools/call", {"name": "lxd_list_instances",
                                                  "arguments": {"cluster_id": "nuc2"}})
    result = response.json()["result"]
    assert result["isError"] is True
    assert "lxd.proxy" in result["content"][0]["text"]
    assert len(calls) == 1


@pytest.mark.parametrize("wire", ["legacy", "modern"])
def test_a_notification_is_accepted_without_a_body(mcp_app, monkeypatch, wire: str) -> None:
    _authenticated_as(monkeypatch, _key(["inventory.read"]))
    headers = {**HEADERS, **CREDENTIAL}
    params: Dict[str, Any] = {}
    if wire == "modern":
        headers.update({"MCP-Protocol-Version": MODERN, "Mcp-Method": "notifications/initialized"})
        params = {"_meta": META}
    with TestClient(mcp_app) as client:
        response = client.post("/api/v1/mcp", headers=headers,
                               json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": params})
    assert response.status_code == 202


def test_a_batch_is_refused(mcp_app, monkeypatch) -> None:
    """A batch would spend one budget token for many calls; the transport refuses it."""
    _authenticated_as(monkeypatch, _key(["inventory.read"]))
    one = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}
    with TestClient(mcp_app) as client:
        _initialize(client)
        response = client.post("/api/v1/mcp", headers={**HEADERS, **CREDENTIAL}, json=[one, {**one, "id": 2}])
    assert response.status_code == 400


# ── the largest documented write fits ──────────────────────────────────────────────────────────

def test_the_body_cap_covers_the_documented_write() -> None:
    limit = request_body_limit()
    assert limit >= DEFAULT_MAX_REQUEST_BODY_SIZE
    assert limit >= 4 * math.ceil(lxd_proxy.FILE_WRITE_LIMIT / 3) + 4096 * 6


def test_the_body_cap_never_drops_below_the_sdk_default(monkeypatch) -> None:
    monkeypatch.setattr(lxd_proxy, "FILE_WRITE_LIMIT", 1024)
    assert request_body_limit() == DEFAULT_MAX_REQUEST_BODY_SIZE


def _files_key() -> Dict[str, Any]:
    return _key(["lxd.proxy", "instance.files"],
                clusters=[{"cluster_id": "nuc2", "permissions": ["write"], "projects": ["*"]}])


def _write(client: TestClient, encoded: str, creds: bool = True):
    return client.post(
        "/api/v1/mcp",
        headers={**HEADERS, **(CREDENTIAL if creds else {})},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "lxd_write_file",
            "arguments": {"cluster_id": "nuc2", "name": "web", "path": "/srv/blob", "content_base64": encoded},
        }},
    )


def test_a_full_size_base64_write_reaches_the_proxy_intact(mcp_app_full, monkeypatch) -> None:
    _authenticated_as(monkeypatch, _files_key())
    seen: List[Any] = []

    async def _proxy(request: Any, **_: Any) -> Any:
        seen.append(request)
        return type("R", (), {"success": True, "data": {"bytes": 0}, "error": None, "error_code": None})()

    monkeypatch.setattr(integrations, "proxy_operation", _proxy)
    encoded = base64.b64encode(b"\x07" * lxd_proxy.FILE_WRITE_LIMIT).decode()
    with TestClient(mcp_app_full) as client:
        response = _write(client, encoded)
    assert response.status_code == 200, response.text[:200]
    assert seen and seen[0].params["content_base64"] == encoded


def test_a_body_over_the_cap_is_413(mcp_app_full, monkeypatch) -> None:
    _authenticated_as(monkeypatch, _files_key())
    with TestClient(mcp_app_full) as client:
        response = _write(client, "A" * (request_body_limit() + 1))
    assert response.status_code == 413


def test_an_oversized_body_without_a_credential_is_401_unread(mcp_app_full, monkeypatch) -> None:
    _authenticated_as(monkeypatch, None)
    with TestClient(mcp_app_full) as client:
        response = _write(client, "A" * (request_body_limit() + 1), creds=False)
    assert response.status_code == 401


# ── the budget headers ─────────────────────────────────────────────────────────────────────────

def test_the_budget_headers_reach_the_client(mcp_app, monkeypatch) -> None:
    async def _validate(x_api_key_id=None, x_api_key_secret=None, response=None, **_):
        response.headers["X-RateLimit-Limit"] = "300"
        response.headers["X-RateLimit-Remaining"] = "299"
        response.headers["X-RateLimit-Reset"] = "60"
        return _key(["inventory.read"])

    monkeypatch.setattr(integrations, "validate_api_key", _validate)
    with TestClient(mcp_app) as client:
        response = _rpc(client, "tools/list")
    assert response.status_code == 200
    assert (response.headers["x-ratelimit-limit"], response.headers["x-ratelimit-remaining"]) == ("300", "299")


def test_no_budget_header_is_invented_when_the_limiter_is_silent(mcp_app, monkeypatch) -> None:
    _authenticated_as(monkeypatch, _key(["inventory.read"]))
    with TestClient(mcp_app) as client:
        response = _rpc(client, "tools/list")
    assert not [h for h in response.headers if h.lower().startswith("x-ratelimit-")]


def test_a_429_keeps_its_retry_after(mcp_app, monkeypatch) -> None:
    async def _spent(**_: Any) -> Any:
        raise HTTPException(status_code=429, detail="Rate limit exceeded",
                            headers={"Retry-After": "41", "X-RateLimit-Remaining": "0"})

    monkeypatch.setattr(integrations, "validate_api_key", _spent)
    with TestClient(mcp_app) as client:
        response = _rpc(client, "tools/list")
    assert response.status_code == 429
    assert response.headers["retry-after"] == "41"


# ── what a project-limited key is offered ──────────────────────────────────────────────────────

CLUSTER_WIDE = {
    "lxd_create_network", "lxd_delete_network", "lxd_create_storage_pool",
    "lxd_delete_storage_pool", "lxd_create_project", "lxd_delete_project",
}


@pytest.mark.parametrize("projects,shown", [(["tenant-a"], False), (["*"], True)])
def test_cluster_wide_tools_need_a_whole_cluster(mcp_app_full, monkeypatch, projects, shown) -> None:
    _authenticated_as(monkeypatch, _key(
        ["inventory.read", "lxd.proxy"],
        clusters=[{"cluster_id": "nuc2", "permissions": ["admin"], "projects": projects}],
    ))
    with TestClient(mcp_app_full) as client:
        _initialize(client)
        names = set(_tool_names(_rpc(client, "tools/list")))
    assert "lxd_delete_instance" in names
    assert (CLUSTER_WIDE <= names) is shown
    assert not (CLUSTER_WIDE & names) or shown


def test_a_key_holding_everything_is_offered_every_tool(mcp_app_full, monkeypatch) -> None:
    from app.mcp.contract import TOOL_CONTRACT

    _authenticated_as(monkeypatch, _key(
        ["inventory.read", "lxd.proxy", "instance.exec", "instance.files",
         "desktop.view", "desktop.input", "desktop.clipboard"],
        clusters=[{"cluster_id": "nuc2", "permissions": ["admin"], "projects": ["*"]}],
    ))
    with TestClient(mcp_app_full) as client:
        _initialize(client)
        names = _tool_names(_rpc(client, "tools/list"))
    assert sorted(names) == sorted(TOOL_CONTRACT)
    assert len(names) == 37
