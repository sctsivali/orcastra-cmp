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

import asyncio

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

def test_the_endpoint_is_served_by_default(monkeypatch) -> None:
    """On by default, because the flag was never the boundary.

    The endpoint refuses every request without a valid integration key, and tools/list is narrowed
    per key, so a key lacking a capability never sees the tools that cost it. What stands in the
    way is the capability, the per-cluster grant, the project allow-list, and for the desktop
    family the owning organization's consent. A flag in front of those adds no safety and invites
    being mistaken for the lock that matters.
    """
    monkeypatch.delenv("MCP_ENABLED", raising=False)
    from app.core.config import get_settings

    get_settings.cache_clear()
    import app.main as main

    app = main.create_app()
    get_settings.cache_clear()
    assert [r for r in app.router.routes if getattr(r, "path", "") == "/api/v1/mcp"]


def test_the_flag_still_turns_it_off(monkeypatch) -> None:
    """An operator who wants the door gone can still remove it."""
    monkeypatch.setenv("MCP_ENABLED", "false")
    from app.core.config import get_settings

    get_settings.cache_clear()
    import app.main as main

    app = main.create_app()
    get_settings.cache_clear()
    assert not [r for r in app.router.routes if getattr(r, "path", "") == "/api/v1/mcp"]


def test_the_sensitive_tiers_are_offered_but_still_gated(monkeypatch) -> None:
    """The tier flags decide what is OFFERED. Whether a key may use it is decided elsewhere, and
    the tests below prove a key without the capability is refused and shown nothing."""
    monkeypatch.delenv("MCP_DESKTOP_TOOLS_ENABLED", raising=False)
    monkeypatch.delenv("MCP_GUEST_TOOLS_ENABLED", raising=False)
    from app.core.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    get_settings.cache_clear()
    assert settings.mcp_desktop_tools_enabled and settings.mcp_guest_tools_enabled


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


@pytest.mark.parametrize("method", ["tools/list", "initialize", "ping"])
def test_a_rejected_credential_is_refused_over_http_on_every_method(
    mcp_app, monkeypatch, method: str
) -> None:
    """A wrong, revoked, expired or deleted key is 401, not a 200 carrying a protocol error.

    This test previously asserted the opposite, and the opposite was live. The endpoint checked
    only that credential headers were PRESENT and left verification to the message loop, whose
    single channel is a JSON-RPC error inside a 200. Two consequences, both measured against the
    running deployment before the fix:

    A client cannot distinguish that from a tool that failed, so it retries a dead credential
    rather than telling its operator to fix the key, and revoking a key stops being an
    unambiguous signal. Revocation is a security control, so its signal has to be legible.

    And `initialize` and `ping` were exempt from authentication altogether, so anyone who could
    reach the port received the server instructions and the whole capability advertisement with
    no valid key at all, on a path that spent no per-key budget. That is why this is
    parametrized over the handshake methods rather than testing `tools/list` alone.
    """
    _authenticated_as(monkeypatch, None)
    with TestClient(mcp_app) as client:
        response = _rpc(client, method)
    assert response.status_code == 401, response.text
    assert response.headers.get("www-authenticate", "").startswith("Bearer")
    assert "tools" not in response.text and "instructions" not in response.text


def test_the_credential_is_verified_once_per_request(mcp_app, monkeypatch) -> None:
    """`validate_api_key` spends the per-key budget, so verifying again in the middleware would
    charge every integration twice for one call and halve its documented allowance. The endpoint
    verifies and leaves the result on the ASGI scope; the middleware reads it."""
    calls: List[str] = []
    real = _key(["inventory.read", "lxd.proxy"])

    async def _counting(x_api_key_id: str = "", x_api_key_secret: str = "", **_: object):
        calls.append(x_api_key_id)
        return real

    import app.routers.integrations as integrations

    monkeypatch.setattr(integrations, "validate_api_key", _counting)
    with TestClient(mcp_app) as client:
        _initialize(client)
        assert len(calls) == 1, "the handshake verified the credential more than once"
        calls.clear()
        response = _rpc(client, "tools/list")
    assert response.status_code == 200
    assert len(calls) == 1, f"one tools/list verified the credential {len(calls)} times"


def test_an_unauthenticated_message_reaching_the_middleware_is_refused(monkeypatch) -> None:
    """The endpoint authenticates first, so the middleware never sees an unverified message
    through the mounted route. It still fails closed rather than serving one, because a gate
    that relies on its caller is the defect class this package keeps finding."""
    from mcp.shared.exceptions import MCPError

    from app.mcp.auth import ApiKeyMiddleware

    class _Ctx:
        method = "tools/list"
        request_id = 1
        params: dict = {}
        request = None

    async def _next(_ctx):  # pragma: no cover - reaching this is the failure
        raise AssertionError("an unauthenticated message reached the handler")

    with pytest.raises(MCPError):
        asyncio.run(ApiKeyMiddleware()(_Ctx(), _next))


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

def _refusal_text(response) -> str:
    """The refusal, from the channel a capability refusal is supposed to take.

    important: an `isError` tool result, NOT a JSON-RPC error. `app/mcp/errors.py` states the
    rule and `dispatch.py` already followed it for the per-cluster 403, while the capability gate
    raised a protocol error for the same class of answer. A protocol error reads to a model as
    "the server broke", so it retries instead of reporting the one thing a human could fix. These
    tests asserted the protocol shape and so held the inconsistency in place.
    """
    body = response.json()
    assert "error" not in body, (
        f"a capability refusal must not take the protocol channel: {json.dumps(body)[:300]}"
    )
    result = body["result"]
    assert result.get("isError") is True, f"not refused: {json.dumps(body)[:300]}"
    return json.dumps(result)


def test_a_tool_call_without_the_capability_is_refused(mcp_app, monkeypatch) -> None:
    _authenticated_as(monkeypatch, _key(["inventory.read"]))
    with TestClient(mcp_app) as client:
        _initialize(client)
        response = _rpc(client, "tools/call", {
            "name": "lxd_list_instances", "arguments": {"cluster_id": "nuc2"}
        })
    text = _refusal_text(response)
    assert "lxd.proxy" in text, (
        "the refusal does not name the capability, so a caller cannot act on it"
    )


def test_an_unknown_tool_is_refused_rather_than_run_ungated(mcp_app, monkeypatch) -> None:
    _authenticated_as(monkeypatch, _key(["inventory.read", "lxd.proxy"]))
    with TestClient(mcp_app) as client:
        _initialize(client)
        response = _rpc(client, "tools/call", {"name": "lxd_not_a_tool", "arguments": {}})
    assert "error" in response.json()


# ── the ratchet: every registered tool is priced ───────────────────────────────────────────────

def _registered_tool_names(*, admin: bool = False, guest: bool = False) -> List[str]:
    import anyio

    from app.mcp.server import build_mcp_server

    mcp = build_mcp_server(
        Settings(MCP_ADMIN_TOOLS_ENABLED=admin, MCP_GUEST_TOOLS_ENABLED=guest)
    )
    return [tool.name for tool in anyio.run(mcp.list_tools)]


@pytest.mark.parametrize(("admin", "guest"), [(False, False), (True, False), (False, True), (True, True)])
def test_every_registered_tool_declares_a_capability(admin: bool, guest: bool) -> None:
    """The MCP analogue of the REST ratchet. A tool without a contract row would authenticate and
    then run with no capability check at all, which is exactly the defect that ratchet exists for.

    Parametrized over the tier flags because the default build registers neither the admin nor the
    guest tier, so checking only the default would leave the eleven most sensitive tools uncovered
    by the test written to cover them.
    """
    missing = sorted(set(_registered_tool_names(admin=admin, guest=guest)) - set(TOOL_CONTRACT))
    assert not missing, f"these tools are registered with no contract row, so they run ungated: {missing}"


def test_the_sensitive_tiers_are_absent_until_they_are_enabled() -> None:
    default = set(_registered_tool_names())
    assert "lxd_delete_project" not in default
    assert "lxd_exec_instance" not in default
    full = set(_registered_tool_names(admin=True, guest=True))
    assert {"lxd_delete_project", "lxd_exec_instance", "lxd_read_file", "lxd_write_file"} <= full


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


# ── the guest tier is never inherited ──────────────────────────────────────────────────────────

@pytest.fixture
def mcp_app_full(monkeypatch):
    """The app with every tier registered, so the guest tools are reachable to be refused."""
    monkeypatch.setenv("MCP_ENABLED", "true")
    monkeypatch.setenv("MCP_ADMIN_TOOLS_ENABLED", "true")
    monkeypatch.setenv("MCP_GUEST_TOOLS_ENABLED", "true")
    from app.core.config import get_settings

    get_settings.cache_clear()
    import app.main as main

    app = main.create_app()
    yield app
    get_settings.cache_clear()


@pytest.mark.parametrize("tool", ["lxd_exec_instance", "lxd_read_file", "lxd_write_file"])
def test_a_legacy_key_is_refused_every_guest_tool(mcp_app_full, monkeypatch, tool: str) -> None:
    """A legacy key reads as unrestricted everywhere else, and must not here.

    `granted_capabilities` returns None for a key issued before the field existed and
    `has_capability` reads None as everything, which is what keeps older integrations working. 17
    of the 22 keys in this deployment are in that state, so the inheriting gate on a guest tool
    would hand root-in-guest to most of the fleet with nobody deciding it.
    """
    _authenticated_as(
        monkeypatch,
        _key(legacy=True, clusters=[{"cluster_id": "nuc2", "permissions": ["admin"]}]),
    )
    arguments = {"cluster_id": "nuc2", "name": "vm-a"}
    if tool == "lxd_exec_instance":
        arguments["command"] = ["id"]
    else:
        arguments["path"] = "/etc/hostname"
    if tool == "lxd_write_file":
        arguments["content"] = "x"

    with TestClient(mcp_app_full) as client:
        _initialize(client)
        response = _rpc(client, "tools/call", {"name": tool, "arguments": arguments})
    text = _refusal_text(response)
    assert "never" in text or "capability" in text, (
        f"the refusal does not explain that the capability is never inherited: {text[:200]}"
    )


def test_a_legacy_key_is_not_shown_the_guest_tools(mcp_app_full, monkeypatch) -> None:
    _authenticated_as(
        monkeypatch,
        _key(legacy=True, clusters=[{"cluster_id": "nuc2", "permissions": ["admin"]}]),
    )
    with TestClient(mcp_app_full) as client:
        _initialize(client)
        names = _tool_names(_rpc(client, "tools/list"))
    assert "lxd_exec_instance" not in names
    assert "lxd_read_file" not in names
    # It still sees the rest, because legacy DOES inherit the powers that existed at issue time.
    assert "lxd_delete_project" in names


def test_a_key_granted_exec_does_not_thereby_get_files(mcp_app_full, monkeypatch) -> None:
    """Holding one guest capability does not imply the other. A health check that runs a command
    has no reason to read files."""
    _authenticated_as(
        monkeypatch,
        _key(["lxd.proxy", "instance.exec"],
             clusters=[{"cluster_id": "nuc2", "permissions": ["admin"]}]),
    )
    with TestClient(mcp_app_full) as client:
        _initialize(client)
        names = _tool_names(_rpc(client, "tools/list"))
    assert "lxd_exec_instance" in names
    assert "lxd_read_file" not in names
    assert "lxd_write_file" not in names
