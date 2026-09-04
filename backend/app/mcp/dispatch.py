"""The one seam between the MCP surface and the REST handlers it wraps.

`proxy_operation` is roughly 380 lines of authorization, breaker check, thread-pool dispatch,
timeout classification, audit, cache invalidation and error taxonomy. Reimplementing any of that
here would guarantee drift, and the half that drifted would be the half nobody tested. So the tools
build the same request model the endpoint takes and call the handler function directly.

important: calling a FastAPI handler as a plain function skips its `Depends` defaults, and two of
them are load-bearing. `require_capability("lxd.proxy")` and `check_read_only_mode` are therefore
invoked explicitly below. Missing the second would mean a read-only deployment still accepted
writes through MCP, which is exactly the kind of gap a second door onto the same API creates.
"""

import logging
from typing import Any, Dict, Optional

from fastapi import HTTPException
from mcp.types import CallToolResult

from app.core.api_key_capabilities import assert_capability
from app.core.dependencies import check_read_only_mode
from app.services.activity_logger import get_activity_logger
from app.services.lxd_service import get_lxd_service

from .auth import current_api_key
from .errors import tool_error, tool_result

logger = logging.getLogger(__name__)

__all__ = ["call_proxy", "run_inventory"]


async def call_proxy(
    operation: str,
    cluster_id: str,
    *,
    project: str = "default",
    params: Optional[Dict[str, Any]] = None,
    wait: bool = True,
) -> CallToolResult:
    """Run one proxy operation as the key that authenticated this message."""
    from app.routers.integrations import ProxyRequest, proxy_operation

    api_key = current_api_key()

    try:
        # The body of `require_capability("lxd.proxy")._dep`, which the direct call bypasses.
        assert_capability(api_key, "lxd.proxy")
        check_read_only_mode()
    except HTTPException as exc:
        return tool_error(str(exc.detail), error_code="forbidden")

    _audit_tool_call(api_key, operation, cluster_id, project, params or {})

    try:
        response = await proxy_operation(
            request=ProxyRequest(
                cluster_id=cluster_id,
                operation=operation,
                project=project,
                params=params or {},
                wait=wait,
            ),
            api_key=api_key,
            lxd_service=get_lxd_service(),
            _=None,
        )
    except HTTPException as exc:
        # A 403 from the per-cluster check or the guest-capability check lands here. It is an
        # answer the model can act on and report, not a broken server, so it takes the tool
        # channel rather than the protocol one.
        return tool_error(str(exc.detail), error_code="forbidden")

    if getattr(response, "success", False):
        return tool_result({"operation": operation, "cluster_id": cluster_id,
                            "data": getattr(response, "data", None)})

    # note: `error_code` is one of PROXY_ERROR_CODES and is the reason it exists. "not_found" and
    # "cluster_unavailable" demand opposite responses from a caller: give up, or retry later.
    # Handing the model the prose alone would make it guess which.
    return tool_error(
        str(getattr(response, "error", "operation failed")),
        error_code=getattr(response, "error_code", None),
    )


async def run_inventory(handler_name: str, **kwargs: Any) -> CallToolResult:
    """Run one inventory read, which fans out across the key's whole grant."""
    import app.routers.integrations as integrations

    api_key = current_api_key()
    try:
        assert_capability(api_key, "inventory.read")
    except HTTPException as exc:
        return tool_error(str(exc.detail), error_code="forbidden")

    handler = getattr(integrations, handler_name)
    try:
        payload = await handler(api_key=api_key, **kwargs)
    except HTTPException as exc:
        return tool_error(str(exc.detail), error_code="forbidden")
    if isinstance(payload, CallToolResult):
        return payload
    return tool_result(payload if isinstance(payload, dict) else {"data": payload})


def _audit_tool_call(
    api_key: Dict[str, Any],
    operation: str,
    cluster_id: str,
    project: str,
    params: Dict[str, Any],
) -> None:
    """Record which tool a model chose, separately from what the key then did to the cluster.

    Two records, two questions. The handler's own `_audit_key_action` answers "what happened to
    this cluster"; this answers "what did the model ask for", which is the one a reviewer needs
    when the concern is an agent behaving unexpectedly rather than a cluster changing.

    note: parameter NAMES only, never values. `params` on a guest-reaching operation carries the
    command or the file path, and on a write carries config the caller chose. The exec path
    already argues this for command output: an audit index that stored it becomes a copy of every
    secret anyone ever read.
    """
    try:
        get_activity_logger().log_activity(
            action="integration.mcp.tool_call",
            target_type="cluster",
            target_id=cluster_id,
            target_host=cluster_id,
            target_project=project,
            details={
                "operation": operation,
                "api_key_id": api_key.get("id"),
                "app_name": api_key.get("app_name"),
                "param_names": sorted(params),
                "via": "mcp",
            },
        )
    except Exception:  # noqa: BLE001 - auditing must never block the operation
        logger.warning("MCP tool-call audit failed for %s on %s", operation, cluster_id)
