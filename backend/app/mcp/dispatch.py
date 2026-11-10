"""The one seam between the MCP surface and the REST handlers it wraps.

`proxy_operation` carries authorization, the breaker check, thread-pool dispatch, timeout
classification, audit, cache invalidation and the error taxonomy. Rather than reimplement any of
it, the tools build the same request model the endpoint takes and call the handler directly.

important: calling a FastAPI handler as a plain function skips its `Depends` defaults, so
`require_capability("lxd.proxy")` and `check_read_only_mode` are invoked explicitly below.
Without the second, a read-only deployment would still accept writes through MCP.
"""

import logging
from typing import Any, Dict, Optional, Tuple

from fastapi import HTTPException
from mcp.types import CallToolResult
from pydantic import BaseModel

from app.core.api_key_capabilities import assert_capability
from app.core.dependencies import check_read_only_mode
from app.services.activity_logger import get_activity_logger
from app.services.lxd_service import get_lxd_service

from .auth import current_api_key, current_tool_name
from .errors import refusal_result, tool_error, tool_result

logger = logging.getLogger(__name__)

__all__ = ["call_proxy", "run_inventory"]

# Parameters whose values name what a call acted on. They identify a resource the same way the
# handler's own audit records do, and none of them carries content a caller chose to send.
_TARGET_PARAMS = ("name", "snapshot_name", "operation_id")


async def call_proxy(
    operation: str,
    cluster_id: str,
    *,
    project: str = "default",
    params: Optional[Dict[str, Any]] = None,
    wait: bool = True,
) -> CallToolResult:
    """Run one proxy operation as the key that authenticated this message."""
    api_key = current_api_key()
    result: Optional[CallToolResult] = None
    try:
        try:
            # The body of `require_capability("lxd.proxy")._dep`, which the direct call bypasses.
            assert_capability(api_key, "lxd.proxy")
            check_read_only_mode()
        except HTTPException as exc:
            result = refusal_result(exc)
            return result
        result = await _run_proxy(api_key, operation, cluster_id, project, params or {}, wait)
        return result
    finally:
        # important: written after the call, whatever happened, so the record says how it ended;
        # written before, a refused delete would be recorded as a success. A call that raised or
        # was cancelled reaches here with no result and is recorded as a failure.
        _audit_tool_call(api_key, operation, cluster_id, project, params or {}, result)


async def _run_proxy(
    api_key: Dict[str, Any],
    operation: str,
    cluster_id: str,
    project: str,
    params: Dict[str, Any],
    wait: bool,
) -> CallToolResult:
    from app.routers.integrations import ProxyRequest, proxy_operation

    try:
        response = await proxy_operation(
            request=ProxyRequest(
                cluster_id=cluster_id,
                operation=operation,
                project=project,
                params=params,
                wait=wait,
            ),
            api_key=api_key,
            lxd_service=get_lxd_service(),
            _=None,
        )
    except HTTPException as exc:
        # A refusal or a failure the model can act on and report, not a broken server, so it takes
        # the tool channel. Its code comes from the status: a 403 is forbidden, but a tripped
        # breaker is cluster_unavailable and a malformed name is invalid_request.
        return refusal_result(exc)

    if getattr(response, "success", False):
        return tool_result({"operation": operation, "cluster_id": cluster_id,
                            "data": getattr(response, "data", None)})

    # `error_code` is one of PROXY_ERROR_CODES. "not_found" and "cluster_unavailable" call for
    # opposite responses (give up, or retry later), and the prose alone would leave the model
    # guessing which.
    return tool_error(
        str(getattr(response, "error", "operation failed")),
        error_code=getattr(response, "error_code", None),
    )


async def run_inventory(
    handler_name: str, *, capability: Optional[str] = "inventory.read", **kwargs: Any
) -> CallToolResult:
    """Run one inventory read, which fans out across the key's whole grant.

    `capability` is the price of the REST route the handler serves. It is None only for whoami,
    whose route takes no capability because it reports what the presented key already holds.
    """
    import app.routers.integrations as integrations

    api_key = current_api_key()
    if capability is not None:
        try:
            assert_capability(api_key, capability)
        except HTTPException as exc:
            return refusal_result(exc)

    handler = getattr(integrations, handler_name)
    try:
        payload = await handler(api_key=api_key, **kwargs)
    except HTTPException as exc:
        return refusal_result(exc)
    if isinstance(payload, CallToolResult):
        return payload
    if isinstance(payload, BaseModel):
        # Dumped here because `tool_result` renders with `json.dumps(default=str)`, which would
        # turn a model into its repr (`valid=True app_name=...`) instead of JSON.
        payload = payload.model_dump(mode="json")
    return tool_result(payload if isinstance(payload, dict) else {"data": payload})


def _outcome(result: Optional[CallToolResult]) -> Tuple[str, Optional[str]]:
    """(result, error_code) for the audit record: success, denied for a refusal, else failure.

    No result means the call raised or was cancelled, and a blocking write may still finish on its
    worker afterwards, so the code says the outcome is unknown rather than that it failed.
    """
    if result is None:
        return "failure", "outcome_unknown"
    if not result.is_error:
        return "success", None
    text = next((c.text for c in result.content if getattr(c, "type", None) == "text"), "")
    code = text[1:text.index("]")] if text.startswith("[") and "]" in text else None
    return ("denied" if code == "forbidden" else "failure"), code


def _audit_tool_call(
    api_key: Dict[str, Any],
    operation: str,
    cluster_id: str,
    project: str,
    params: Dict[str, Any],
    result: Optional[CallToolResult],
) -> None:
    """Record which tool a model chose and how it ended, separately from what the key then did.

    The handler's own `_audit_key_action` records what happened to the cluster, where it writes
    one; this records what the model asked for, which a reviewer needs when an agent behaves
    unexpectedly.

    Parameter names only, plus the values in `_TARGET_PARAMS`. `params` on a guest-reaching
    operation carries the command or the file path, and on a write carries config the caller chose;
    an audit index that stored them would become a copy of every secret anyone read.
    """
    outcome, error_code = _outcome(result)
    targets = {
        field: str(params[field])[:255]
        for field in _TARGET_PARAMS
        if isinstance(params.get(field), (str, int))
    }
    try:
        get_activity_logger().log_activity(
            action="integration.mcp.tool_call",
            target_type="cluster",
            target_id=cluster_id,
            target_host=cluster_id,
            target_project=project,
            result=outcome,
            error_code=error_code,
            details={
                "tool": current_tool_name(),
                "operation": operation,
                "api_key_id": api_key.get("id"),
                "app_name": api_key.get("app_name"),
                "param_names": sorted(params),
                **targets,
                "via": "mcp",
            },
        )
    except Exception:  # noqa: BLE001 - auditing must never block the operation
        logger.warning("MCP tool-call audit failed for %s on %s", operation, cluster_id)
