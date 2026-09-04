"""The bridge between MCP's message loop and this deployment's API-key credential.

MCP's spec-blessed authorization is OAuth 2.1. This deployment authenticates integrations with an
HMAC-verified key pair in two headers, and the whole capability system is built on the metadata
that check returns. So the bridge authenticates every inbound message against the existing
`validate_api_key` and enforces the tool contract before the handler runs.

important: `validate_api_key` is called, not reimplemented. Its parameters carry `Header(...)`
DEFAULTS, so invoking it with keyword arguments runs the identical body: the HMAC digest, the
30-second credential cache, the offload onto `auth_executor`, the revoked, inactive and expiry
checks, and `consume_api_key_budget`. That last one matters most. It means the MCP surface
inherits the per-key rate limit rather than quietly sitting outside it, and it keeps the deliberate
ordering that spends budget only AFTER the secret verifies, so a public key id cannot be used to
exhaust a named integration's allowance.

note: a middleware runs before params validation and wraps every inbound message, which is what
makes this the right layer. The alternative is a check inside each tool function, and that is the
shape `test_every_external_endpoint_declares_a_capability` exists to prevent on the REST side: a
new tool that forgets it is reachable by any key that authenticates.
"""

import logging
from contextvars import ContextVar
from typing import Any, Dict, Optional, Tuple

from fastapi import HTTPException
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, METHOD_NOT_FOUND

from app.core.api_key_capabilities import (
    assert_capability,
    assert_explicit_capability,
    has_capability,
    has_explicit_capability,
)
from app.core.api_key_permissions import PERMISSION_LEVELS, granted_level

from .contract import requirement_for
from .errors import to_mcp_error

logger = logging.getLogger(__name__)

__all__ = ["ApiKeyMiddleware", "current_api_key", "credentials_from_headers"]

# The validated key for the message being handled. Tools read it rather than taking it as an
# argument, because a tool argument is model-supplied and this must not be.
_CURRENT_KEY: ContextVar[Optional[Dict[str, Any]]] = ContextVar("mcp_current_api_key", default=None)

# Handled before a credential exists, or carrying none of their own. `initialize` is the handshake
# and the SDK processes it inline; refusing it before the client has been told what this server
# wants would make the failure unreadable. Notifications carry no request id and expect no reply.
_UNAUTHENTICATED_METHODS = frozenset({"initialize", "ping"})


def current_api_key() -> Dict[str, Any]:
    """The key that authenticated this message. Raises if called outside a handled message."""
    key = _CURRENT_KEY.get()
    if key is None:
        raise MCPError(
            code=INVALID_PARAMS,
            message="no authenticated API key on this request",
        )
    return key


def credentials_from_headers(headers: Any) -> Tuple[Optional[str], Optional[str]]:
    """The key pair, in either accepted spelling.

    Native `X-API-Key-ID` + `X-API-Key-Secret` is preferred over `Authorization: Bearer id:secret`
    when both are present, so a stale Bearer left in a config file cannot shadow an explicit
    credential the operator just set.

    note: never a query parameter. The transport spec forbids an access token in the URI, and this
    deployment's access log records full paths, so a token there would be written to the log index
    on every call.
    """
    if headers is None:
        return None, None
    key_id = headers.get("x-api-key-id") or headers.get("X-API-Key-ID")
    secret = headers.get("x-api-key-secret") or headers.get("X-API-Key-Secret")
    if key_id and secret:
        return key_id, secret

    authorization = headers.get("authorization") or headers.get("Authorization") or ""
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        # Split on the FIRST colon. An `oas_` secret contains none, so this is unambiguous, and
        # splitting on the last would corrupt any future secret that did.
        head, separator, tail = token.partition(":")
        if separator and head and tail:
            return head, tail
    return None, None


def _max_granted_level(api_key: Dict[str, Any]) -> int:
    """The strongest level this key holds on any cluster.

    Used only to decide what to SHOW in tools/list. The real check is per-cluster and happens in
    the handler, because a key may hold admin on one cluster and read on another.
    """
    grants = api_key.get("cluster_permissions") or []
    levels = [
        granted_level(grant.get("permissions"))
        for grant in grants
        if isinstance(grant, dict)
    ]
    return max(levels) if levels else -1


def _may_see(api_key: Dict[str, Any], tool_name: str) -> bool:
    """Whether a tool belongs in this key's tools/list.

    Hiding is a convenience, never the boundary: every tool re-checks on call. The point is that a
    read-only key is not shown `lxd_delete_project`, so the model does not plan around a tool it
    will be refused.
    """
    requirement = requirement_for(tool_name)
    if requirement is None:
        return False
    if not has_capability(api_key, requirement.capability):
        return False
    guest = requirement.guest_capability
    if guest is not None and not has_explicit_capability(api_key, guest):
        return False
    if requirement.level is not None:
        if _max_granted_level(api_key) < PERMISSION_LEVELS.get(requirement.level, 0):
            return False
    return True


def _enforce_tool_call(ctx: ServerRequestContext[Any, Any], api_key: Dict[str, Any]) -> None:
    """The gate, before the handler runs.

    The per-cluster level and the project allow-list are NOT checked here. They are checked by the
    handler this dispatches into, against the cluster named in the arguments, which is the only
    place that knows which cluster it is. Duplicating them here would be a second implementation
    of a rule that already has one.
    """
    params = ctx.params or {}
    tool_name = params.get("name")
    if not isinstance(tool_name, str):
        raise MCPError(code=INVALID_PARAMS, message="tools/call requires a tool name")

    requirement = requirement_for(tool_name)
    if requirement is None:
        # A tool the server registered without a contract row would otherwise run ungated. Refusing
        # here rather than in the handler means a missing row fails closed.
        raise MCPError(
            code=METHOD_NOT_FOUND,
            message=f"unknown tool: {tool_name}",
        )

    try:
        assert_capability(api_key, requirement.capability)
        guest = requirement.guest_capability
        if guest is not None:
            # important: the EXPLICIT variant. `assert_capability` reads an absent capabilities
            # field as unrestricted, which is what keeps older integrations working and is the
            # wrong answer for a power that reaches inside a guest. Most keys in this deployment
            # are in that state, so the inheriting form here would hand guest access to nearly the
            # whole fleet with nobody deciding it.
            assert_explicit_capability(api_key, guest)
    except HTTPException as exc:
        logger.warning(
            "MCP refused %s for key %s: %s",
            tool_name, api_key.get("id", "unknown"), exc.detail,
        )
        raise to_mcp_error(exc) from None


class ApiKeyMiddleware:
    """Authenticates every message, gates every tool call, narrows every tool list."""

    async def __call__(
        self, ctx: ServerRequestContext[Any, Any], call_next: CallNext
    ) -> HandlerResult:
        if ctx.request_id is None or ctx.method in _UNAUTHENTICATED_METHODS:
            return await call_next(ctx)

        api_key = await self._authenticate(ctx)
        token = _CURRENT_KEY.set(api_key)
        try:
            if ctx.method == "tools/call":
                _enforce_tool_call(ctx, api_key)
            result = await call_next(ctx)
            if ctx.method == "tools/list":
                result = self._narrow_tool_list(result, api_key)
            return result
        finally:
            _CURRENT_KEY.reset(token)

    async def _authenticate(self, ctx: ServerRequestContext[Any, Any]) -> Dict[str, Any]:
        # The transport attaches the HTTP request to every message, so the credential is available
        # on tools/call and not only at handshake. That is what lets this server run stateless.
        request = getattr(ctx, "request", None)
        key_id, secret = credentials_from_headers(getattr(request, "headers", None))
        if not key_id or not secret:
            raise MCPError(
                code=INVALID_PARAMS,
                message=(
                    "missing API key. Send X-API-Key-ID and X-API-Key-Secret headers, or "
                    "Authorization: Bearer <key-id>:<key-secret>."
                ),
            )

        from app.routers.integrations import validate_api_key

        try:
            return await validate_api_key(x_api_key_id=key_id, x_api_key_secret=secret)
        except HTTPException as exc:
            # note: the key id is logged and the secret never is, matching the REST surface. A
            # denial on this router is the only trace a key-authenticated caller leaves.
            logger.warning("MCP authentication failed for key %s: %s", key_id, exc.status_code)
            raise to_mcp_error(exc) from None

    @staticmethod
    def _narrow_tool_list(result: Any, api_key: Dict[str, Any]) -> Any:
        """Drop tools this key cannot use from a tools/list reply.

        note: `HandlerResult` is `BaseModel | dict | None`, and the SDK returns the dict form here,
        so handling only the attribute form silently did nothing. That failure is invisible from
        the outside: the list is merely wider than it should be, every tool still re-checks on
        call, and nothing errors. The tests below are what caught it.
        """
        if isinstance(result, dict):
            tools = result.get("tools")
            if not isinstance(tools, list):
                return result
            visible = [
                tool
                for tool in tools
                if _may_see(api_key, (tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", "")))
            ]
            if len(visible) == len(tools):
                return result
            narrowed = dict(result)
            narrowed["tools"] = visible
            return narrowed

        tools = getattr(result, "tools", None)
        if not isinstance(tools, list):
            return result
        visible = [tool for tool in tools if _may_see(api_key, getattr(tool, "name", ""))]
        if len(visible) == len(tools):
            return result
        return result.model_copy(update={"tools": visible})
