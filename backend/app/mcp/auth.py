"""The bridge between MCP's message loop and this deployment's API-key credential.

MCP specifies OAuth 2.1; integrations here use an HMAC-verified key pair in two headers, with
capabilities in the key's metadata. The transport endpoint (`mount.py`) verifies the key once per
HTTP request. This middleware reads that verdict for every message, refuses unserved methods and
enforces the tool contract before params validation, so a new tool cannot skip the gate.

important: `validate_api_key` runs exactly once, in `mount.py`, and is never reimplemented. Called
with keyword arguments (its parameters carry `Header(...)` defaults) it runs the whole body,
`consume_api_key_budget` included; validating again would charge the per-key budget twice.
"""

import logging
from contextvars import ContextVar
from typing import Any, Dict, Optional, Tuple

from fastapi import HTTPException
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, METHOD_NOT_FOUND, CallToolResult

from app.core.api_key_capabilities import (
    assert_capability,
    assert_explicit_capability,
    has_capability,
    has_explicit_capability,
    project_grant_is_unrestricted,
)
from app.core.api_key_permissions import PERMISSION_LEVELS, granted_level

from .contract import requirement_for
from .errors import tool_error
from .scope import SCOPE_API_KEY

logger = logging.getLogger(__name__)

__all__ = [
    "SERVED_METHODS",
    "ApiKeyMiddleware",
    "a_granted_project",
    "credentials_from_headers",
    "current_api_key",
    "current_tool_name",
]

# The validated key for the message being handled. Tools read it rather than taking it as an
# argument, because a tool argument is model-supplied and this must not be.
_CURRENT_KEY: ContextVar[Optional[Dict[str, Any]]] = ContextVar("mcp_current_api_key", default=None)

# The tool a tools/call named, for the audit record. Set here because only the message knows it:
# the dispatchers see an operation, and three desktop tools share one.
_CURRENT_TOOL: ContextVar[Optional[str]] = ContextVar("mcp_current_tool", default=None)

# Every request method this server answers. Anything else is refused before the SDK sees it.
#
# important: an allow-list. SDK 2.2 registers `subscriptions/listen` on every server, and on
# protocol revision `2026-07-28` it is served as an SSE stream that pings every 15 s until the
# client hangs up, whatever `json_response` says. Nothing here publishes to it, so any
# authenticated key could hold connection slots open until a worker's `--limit-concurrency` is
# full. A default handler a later SDK release adds is refused the same way. The resource and
# prompt lists stay, empty, because clients ask for them after the handshake and treat an error
# as a broken server.
SERVED_METHODS = frozenset({
    "initialize",
    "ping",
    "server/discover",
    "tools/list",
    "tools/call",
    "resources/list",
    "resources/templates/list",
    "prompts/list",
})

# Notifications carry no request id and expect no reply, so there is no channel to refuse them
# on. They are still authenticated: the endpoint verifies the credential before the manager sees
# any message, `initialize` and `ping` included, so nothing is exempt.


def current_api_key() -> Dict[str, Any]:
    """The key that authenticated this message. Raises if called outside a handled message."""
    key = _CURRENT_KEY.get()
    if key is None:
        raise MCPError(
            code=INVALID_PARAMS,
            message="no authenticated API key on this request",
        )
    return key


def a_granted_project(cluster_id: str) -> str:
    """A project this key may name on `cluster_id`, for a call whose answer does not depend on it.

    `proxy_operation` and the inventory routes authorise the request's project before anything
    else, so a fixed `default` would be refused for every key limited to other projects. Used where
    the project only has to pass that check: listing projects (narrowed to the grant whichever is
    named) and polling an operation (answered by the operation's own owner whatever `project` is).
    """
    for grant in current_api_key().get("cluster_permissions") or []:
        if not isinstance(grant, dict) or grant.get("cluster_id") != cluster_id:
            continue
        projects = grant.get("projects", ["default"]) or ["default"]
        if project_grant_is_unrestricted(projects) or "default" in projects:
            return "default"
        return sorted(str(p) for p in projects)[0]
    # No grant on that cluster: the route refuses it by name, which is the useful answer.
    return "default"


def current_tool_name() -> Optional[str]:
    """The tool the message being handled called, or None outside a tools/call."""
    return _CURRENT_TOOL.get()


def credentials_from_headers(headers: Any) -> Tuple[Optional[str], Optional[str]]:
    """The key pair, in either accepted spelling.

    Native `X-API-Key-ID` + `X-API-Key-Secret` is preferred over `Authorization: Bearer id:secret`
    when both are present, so a stale Bearer left in a config file cannot shadow an explicit
    credential the operator just set.

    Never a query parameter: the transport spec forbids an access token in the URI, and the access
    log records full paths, so a token there would be written to the log index on every call.
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
        # Split on the first colon. An `oas_` secret contains none, so this is unambiguous, and
        # splitting on the last would corrupt any future secret that did.
        head, separator, tail = token.partition(":")
        if separator and head and tail:
            return head, tail
    return None, None


def _max_granted_level(api_key: Dict[str, Any]) -> int:
    """The strongest level this key holds on any cluster.

    Used only to decide what to show in tools/list. The real check is per-cluster and happens in
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

    Hiding is a convenience, never the boundary: every tool re-checks on call. A read-only key is
    not shown `lxd_delete_project`, so the model does not plan around a tool it will be refused.
    """
    requirement = requirement_for(tool_name)
    if requirement is None:
        return False
    if requirement.capability is not None and not has_capability(api_key, requirement.capability):
        return False
    guest = requirement.guest_capability
    if guest is not None and not has_explicit_capability(api_key, guest):
        return False
    if requirement.level is not None:
        if _max_granted_level(api_key) < PERMISSION_LEVELS.get(requirement.level, 0):
            return False
    if requirement.cluster_wide and not _holds_a_whole_cluster(api_key, requirement.level):
        return False
    return True


def _holds_a_whole_cluster(api_key: Dict[str, Any], level: Optional[str]) -> bool:
    """Whether some grant reaches `level` on a cluster with every project (`*`).

    A cluster-wide operation targets something no project contains, so `proxy_operation` refuses
    it to a key limited to named projects whatever its level. Without this a project-limited admin
    key would be shown cluster-wide tools it can never use.
    """
    needed = PERMISSION_LEVELS.get(level or "", 0)
    for grant in api_key.get("cluster_permissions") or []:
        if not isinstance(grant, dict):
            continue
        unrestricted = project_grant_is_unrestricted(grant.get("projects", ["default"]))
        if unrestricted and granted_level(grant.get("permissions")) >= needed:
            return True
    return False


def _enforce_tool_call(
    ctx: ServerRequestContext[Any, Any], api_key: Dict[str, Any]
) -> Optional[CallToolResult]:
    """The gate, before the handler runs. Returns a refusal to send back, or None to proceed.

    The per-cluster level and the project allow-list are not checked here: the handler this
    dispatches into checks them against the cluster named in the arguments, the only place that
    knows which cluster it is.

    A capability refusal takes the tool channel, as the per-cluster 403 does in `dispatch.py`
    (`errors.py` states the rule), so the model can read why it was refused whichever check
    caught it. An unknown tool stays a protocol error, because that request is malformed
    rather than refused.
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
        if requirement.capability is not None:
            assert_capability(api_key, requirement.capability)
        guest = requirement.guest_capability
        if guest is not None:
            # important: the explicit variant. `assert_capability` reads an absent capabilities
            # field as unrestricted, which keeps older integrations working but would hand guest
            # access to every key that never declared capabilities.
            assert_explicit_capability(api_key, guest)
    except HTTPException as exc:
        logger.warning(
            "MCP refused %s for key %s: %s",
            tool_name, api_key.get("id", "unknown"), exc.detail,
        )
        return tool_error(str(exc.detail), error_code="forbidden")
    return None


class ApiKeyMiddleware:
    """Authenticates every message, gates every tool call, narrows every tool list."""

    async def __call__(
        self, ctx: ServerRequestContext[Any, Any], call_next: CallNext
    ) -> HandlerResult:
        api_key = self._authenticated_key(ctx)
        if api_key is None:
            # Unreachable through the mounted route, which authenticates first. Fail closed for
            # anything else driving this server: an unauthenticated message is refused rather
            # than served, except a notification, which has no reply channel to refuse on.
            if ctx.request_id is None:
                return await call_next(ctx)
            raise MCPError(
                code=INVALID_PARAMS,
                message=(
                    "missing API key. Send X-API-Key-ID and X-API-Key-Secret headers, or "
                    "Authorization: Bearer <key-id>:<key-secret>."
                ),
            )

        if ctx.request_id is not None and ctx.method not in SERVED_METHODS:
            raise MCPError(code=METHOD_NOT_FOUND, message=f"method not served: {ctx.method}")

        token = _CURRENT_KEY.set(api_key)
        tool_token = _CURRENT_TOOL.set(None)
        try:
            if ctx.method == "tools/call":
                params = ctx.params or {}
                name = params.get("name") if isinstance(params, dict) else None
                _CURRENT_TOOL.set(name if isinstance(name, str) else None)
                refusal = _enforce_tool_call(ctx, api_key)
                if refusal is not None:
                    return refusal
            result = await call_next(ctx)
            if ctx.method == "tools/list":
                result = self._narrow_tool_list(result, api_key)
            return result
        finally:
            _CURRENT_TOOL.reset(tool_token)
            _CURRENT_KEY.reset(token)

    @staticmethod
    def _authenticated_key(ctx: ServerRequestContext[Any, Any]) -> Optional[Dict[str, Any]]:
        """The key the endpoint already verified for this HTTP request.

        important: this reads the verdict and never validates again. `validate_api_key` spends
        the per-key budget, so a second call would charge every integration twice for one call.
        The transport attaches the HTTP request to every message, so one verification covers a
        stateless server.
        """
        request = getattr(ctx, "request", None)
        scope = getattr(request, "scope", None)
        if not isinstance(scope, dict):
            return None
        api_key = scope.get(SCOPE_API_KEY)
        return api_key if isinstance(api_key, dict) else None

    @staticmethod
    def _narrow_tool_list(result: Any, api_key: Dict[str, Any]) -> Any:
        """Drop tools this key cannot use from a tools/list reply.

        `HandlerResult` is `BaseModel | dict | None`, and the SDK returns the dict form here, so
        both forms are handled. Missing one fails silently: the list is only wider than it should
        be, since every tool still re-checks on call.
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
