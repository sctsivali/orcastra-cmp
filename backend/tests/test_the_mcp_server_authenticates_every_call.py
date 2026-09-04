"""The MCP surface is a second door onto the same API keys, so it needs the same gate.

`external_router` learned the hard way that authentication alone is not a gate: every valid key
reached every endpoint on it, so a key issued for a read-only integration could mint an
organization. `test_every_external_endpoint_declares_a_capability` is the ratchet that keeps a new
REST endpoint from repeating it. An MCP tool is a new endpoint by any other name, and it matches
neither that test's filename allow-list nor its `router.` decorator filter, so it would have had
no coverage at all. This file is the MCP half of that ratchet, plus the behaviour it protects.
"""

import json
from typing import Any, Dict, List, Optional

import pytest
from fastapi import HTTPException
from starlette.testclient import TestClient

from app.core.config import Settings
from app.mcp.contract import TOOL_CONTRACT
from app.services.lxd.proxy_operations import (
    GUEST_ACCESS_OPERATIONS,
    PROXY_OPERATION_LEVELS,
)

HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
CREDENTIAL = {"X-API-Key-ID": "oak_test", "X-API-Key-Secret": "oas_test"}


def _key(
    capabilities: Optional[List[str]] = None,
    *,
    legacy: bool = False,
    clusters: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    key: Dict[str, Any] = {
        "id": "oak_test",
        "app_name": "mcp-test",
        "cluster_permissions": clusters if clusters is not None else [
            {"cluster_id": "nuc2", "permissions": ["read"]}
        ],
    }
    if not legacy:
        key["capabilities"] = capabilities if capabilities is not None else []
    return key


@pytest.fixture
def mcp_app(monkeypatch):
    """The real app with MCP on, and no other change."""
    monkeypatch.setenv("MCP_ENABLED", "true")
    from app.core.config import get_settings

    get_settings.cache_clear()
    import app.main as main

    app = main.create_app()
    yield app
    get_settings.cache_clear()


def _authenticated_as(monkeypatch, api_key: Optional[Dict[str, Any]], status: int = 401):
    """Point the middleware's credential check at a fixed answer."""
    import app.routers.integrations as integrations

    async def _validate(x_api_key_id=None, x_api_key_secret=None, **_):
        if api_key is None:
            raise HTTPException(status_code=status, detail="Invalid API key")
        return api_key

    monkeypatch.setattr(integrations, "validate_api_key", _validate)


def _rpc(client: TestClient, method: str, params: Optional[Dict[str, Any]] = None, *, creds=True):
    headers = {**HEADERS, **(CREDENTIAL if creds else {})}
    return client.post(
        "/api/v1/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )


def _initialize(client: TestClient) -> None:
    _rpc(client, "initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    })


# ── the endpoint exists only when it is turned on ──────────────────────────────────────────────

def test_the_endpoint_is_absent_until_it_is_enabled(monkeypatch) -> None:
    """A second door onto the production API keys should be a decision, not a deploy side effect."""
    monkeypatch.delenv("MCP_ENABLED", raising=False)
    from app.core.config import get_settings

    get_settings.cache_clear()
    import app.main as main

    app = main.create_app()
    get_settings.cache_clear()
    assert not [r for r in app.router.routes if getattr(r, "path", "") == "/api/v1/mcp"]


def test_the_endpoint_is_mounted_when_enabled(mcp_app) -> None:
    assert [r for r in mcp_app.router.routes if getattr(r, "path", "") == "/api/v1/mcp"]


# ── the credential ─────────────────────────────────────────────────────────────────────────────

def test_a_request_with_no_credential_is_refused_over_http(mcp_app) -> None:
    """HTTP 401, not a JSON-RPC error. A missing credential is the caller's operator to fix, and
    an MCP client surfaces a transport status where it may swallow a protocol error."""
    with TestClient(mcp_app) as client:
        response = _rpc(client, "initialize", creds=False)
    assert response.status_code == 401
    assert response.headers.get("www-authenticate", "").startswith("Bearer")


def test_the_bearer_spelling_is_accepted(mcp_app, monkeypatch) -> None:
    """Some clients only offer an Authorization field."""
    _authenticated_as(monkeypatch, _key(["inventory.read"]))
    with TestClient(mcp_app) as client:
        response = client.post(
            "/api/v1/mcp",
            headers={**HEADERS, "Authorization": "Bearer oak_test:oas_test"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": "t", "version": "0"}}},
        )
    assert response.status_code == 200


def test_a_rejected_credential_does_not_reach_the_tool_layer(mcp_app, monkeypatch) -> None:
    _authenticated_as(monkeypatch, None)
    with TestClient(mcp_app) as client:
        _initialize(client)
        response = _rpc(client, "tools/list")
    assert response.status_code == 200
    assert "error" in response.json(), "an invalid key still got a tool list"


# ── the verbs this server does not serve ───────────────────────────────────────────────────────

@pytest.mark.parametrize("verb", ["get", "delete"])
def test_the_streaming_verbs_are_refused(mcp_app, verb: str) -> None:
    """In stateless mode a standalone SSE stream is one nothing can be written to, so honouring
    GET would hold a concurrency slot per client forever for no function. The spec permits 405."""
    with TestClient(mcp_app) as client:
        response = getattr(client, verb)("/api/v1/mcp", headers={**HEADERS, **CREDENTIAL})
    assert response.status_code == 405


# ── the tool list is narrowed to what the key may use ──────────────────────────────────────────

def _tool_names(response) -> List[str]:
    payload = response.json()
    return sorted(tool["name"] for tool in payload["result"]["tools"])


def test_a_read_only_key_is_not_shown_the_write_tools(mcp_app, monkeypatch) -> None:
    """Hiding is a convenience and never the boundary, but a model should not plan around a tool
    it will be refused."""
    _authenticated_as(monkeypatch, _key(["inventory.read", "lxd.proxy"]))
    with TestClient(mcp_app) as client:
        _initialize(client)
        names = _tool_names(_rpc(client, "tools/list"))
    assert "lxd_list_instances" in names
    assert "orcastra_whoami" in names


def test_a_key_without_the_proxy_capability_sees_only_inventory(mcp_app, monkeypatch) -> None:
    _authenticated_as(monkeypatch, _key(["inventory.read"]))
    with TestClient(mcp_app) as client:
        _initialize(client)
        names = _tool_names(_rpc(client, "tools/list"))
    assert names, "an inventory-only key was shown nothing at all"
    assert all(name.startswith("orcastra_") for name in names), (
        f"a key without lxd.proxy was shown proxy tools: {names}"
    )


def test_a_key_with_an_empty_grant_is_shown_no_proxy_tools(mcp_app, monkeypatch) -> None:
    """An explicit empty list is a decision the operator expressed, and it grants nothing."""
    _authenticated_as(monkeypatch, _key([], clusters=[]))
    with TestClient(mcp_app) as client:
        _initialize(client)
        names = _tool_names(_rpc(client, "tools/list"))
    assert names == [], f"a key granted nothing was shown {names}"


# ── calling a tool the key may not use ─────────────────────────────────────────────────────────

def test_a_tool_call_without_the_capability_is_refused(mcp_app, monkeypatch) -> None:
    _authenticated_as(monkeypatch, _key(["inventory.read"]))
    with TestClient(mcp_app) as client:
        _initialize(client)
        response = _rpc(client, "tools/call", {
            "name": "lxd_list_instances", "arguments": {"cluster_id": "nuc2"}
        })
    body = response.json()
    assert "error" in body
    assert "lxd.proxy" in json.dumps(body), (
        "the refusal does not name the capability, so a caller cannot act on it"
    )


def test_an_unknown_tool_is_refused_rather_than_run_ungated(mcp_app, monkeypatch) -> None:
    _authenticated_as(monkeypatch, _key(["inventory.read", "lxd.proxy"]))
    with TestClient(mcp_app) as client:
        _initialize(client)
        response = _rpc(client, "tools/call", {"name": "lxd_not_a_tool", "arguments": {}})
    assert "error" in response.json()


# ── the ratchet: every registered tool is priced ───────────────────────────────────────────────

def _registered_tool_names(app) -> List[str]:
    import anyio

    from app.mcp.server import build_mcp_server

    mcp = build_mcp_server(Settings())
    tools = anyio.run(mcp.list_tools)
    return [tool.name for tool in tools]


def test_every_registered_tool_declares_a_capability() -> None:
    """The MCP analogue of the REST ratchet. A tool without a contract row would authenticate and
    then run with no capability check at all, which is exactly the defect that ratchet exists for.
    """
    missing = sorted(set(_registered_tool_names(None)) - set(TOOL_CONTRACT))
    assert not missing, f"these tools are registered with no contract row, so they run ungated: {missing}"


def test_no_contract_row_prices_an_operation_that_does_not_exist() -> None:
    unknown = sorted(
        name
        for name, requirement in TOOL_CONTRACT.items()
        if requirement.operation is not None
        and requirement.operation not in PROXY_OPERATION_LEVELS
    )
    assert not unknown, f"these tools name operations the proxy does not serve: {unknown}"


def test_a_tool_never_prices_an_operation_below_what_the_proxy_charges() -> None:
    """The level is derived rather than restated, so this pins the derivation rather than a copy."""
    for name, requirement in TOOL_CONTRACT.items():
        if requirement.operation is None:
            continue
        assert requirement.level == PROXY_OPERATION_LEVELS[requirement.operation], name


def test_every_guest_reaching_tool_carries_its_explicit_capability() -> None:
    """Derived from GUEST_ACCESS_OPERATIONS, so a new guest-reaching operation cannot be exposed
    as a tool without the capability that is never inherited by a legacy key."""
    for name, requirement in TOOL_CONTRACT.items():
        expected = GUEST_ACCESS_OPERATIONS.get(requirement.operation or "")
        assert requirement.guest_capability == expected, name


def test_the_destructive_tools_are_marked_as_such() -> None:
    """A host can only offer a sensible confirmation prompt if the server says which is which."""
    for name, requirement in TOOL_CONTRACT.items():
        if requirement.level == "admin":
            assert requirement.destructive, f"{name} deletes and is not marked destructive"
        if requirement.read_only:
            assert not requirement.destructive, f"{name} is marked both read-only and destructive"


def test_the_raw_proxy_is_not_exposed_as_a_tool() -> None:
    """`/raw-proxy` is an arbitrary-method arbitrary-path escape hatch. Handing it to a model
    re-creates the ungated generic tool with no schema at all."""
    assert not [name for name in TOOL_CONTRACT if "raw" in name.lower()]
