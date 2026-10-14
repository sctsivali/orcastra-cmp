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

# Parameters whose VALUES name what a call acted on. They identify a resource the same way the
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
        # important: AFTER the call and whatever happened, so the record says how it ended. It
        # used to be written before the call with the logger's default result, so a refused
        # delete was recorded as a successful one. A call that raised or was cancelled reaches
        # here with no result and is recorded as a failure.
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

    # note: `error_code` is one of PROXY_ERROR_CODES and is the reason it exists. "not_found" and
    # "cluster_unavailable" demand opposite responses from a caller: give up, or retry later.
    # Handing the model the prose alone would make it guess which.
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
        # note: dumped here because `tool_result` renders with `json.dumps(default=str)`, which
        # turns a model into its repr. whoami's text read `valid=True app_name=...` rather than
        # JSON, so the one tool a model is told to start with answered in Python syntax.
        payload = payload.model_dump(mode="json")
    return tool_result(payload if isinstance(payload, dict) else {"data": payload})


def _outcome(result: Optional[CallToolResult]) -> Tuple[str, Optional[str]]:
    """(result, error_code) for the audit record: success, denied for a refusal, else failure.

    note: no result means the call raised or was cancelled, and a blocking write may still finish
    on its worker afterwards, so the code says the outcome is unknown rather than that it failed.
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

    Two records, two questions. The handler's own `_audit_key_action` answers "what happened to
    this cluster" where it writes one; this answers "what did the model ask for", which is the one
    a reviewer needs when the concern is an agent behaving unexpectedly rather than a cluster
    changing.

    note: parameter NAMES, plus only the values in `_TARGET_PARAMS`. `params` on a guest-reaching
    operation carries the command or the file path, and on a write carries config the caller chose.
    The exec path already argues this for command output: an audit index that stored it becomes a
    copy of every secret anyone ever read.
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
