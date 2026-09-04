"""Which channel a failure takes, decided in one place.

MCP has two, and they mean different things to a model. Getting this wrong is not cosmetic: a
403 rendered as a protocol error is invisible to the model as anything but "the server broke", so
it retries blindly instead of reporting something a human can fix.

    transport / protocol failure  ->  HTTP status, or a JSON-RPC error
    authorization or cluster failure the model can act on  ->  isError CallToolResult

The rule of thumb: if the answer is "your credential is wrong", the caller's operator has to fix
it and the model cannot, so it belongs on the protocol channel. If the answer is "this key may not
do that to that cluster" or "the cluster is down", the model should read it, stop, and say so.
"""

import logging
from typing import Any, Dict, Optional

from fastapi import HTTPException
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, CallToolResult, TextContent

logger = logging.getLogger(__name__)

__all__ = [
    "AUTH_STATUSES",
    "tool_error",
    "tool_result",
    "to_mcp_error",
]

# Statuses that mean the credential itself is wrong rather than the request. These reach the
# caller as a protocol failure, because no amount of the model rewording its arguments fixes them.
AUTH_STATUSES = frozenset({401, 403, 429})


def tool_error(message: str, *, error_code: Optional[str] = None) -> CallToolResult:
    """A failure the model should read and act on.

    note: `is_error=True` rather than a raised MCPError. The model sees the text either way, but
    only this form lets it keep going and report, which is the behaviour wanted for "this key
    holds read on that cluster" or "the cluster is unreachable".
    """
    prefix = f"[{error_code}] " if error_code else ""
    return CallToolResult(
        content=[TextContent(type="text", text=f"{prefix}{message}")],
        is_error=True,
    )


def tool_result(payload: Dict[str, Any]) -> CallToolResult:
    """A success, with the structured payload alongside the text rendering.

    Both halves on purpose: `structured_content` is what a programmatic caller reads, and the text
    is what a model reads. Sending only one makes the tool worse for whichever consumer it is not.
    """
    import json

    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, indent=2, default=str))],
        structured_content=payload,
        is_error=False,
    )


def to_mcp_error(exc: HTTPException) -> MCPError:
    """Render a credential failure as a protocol error.

    The detail is preserved verbatim where it is a string, because the messages this surface
    already produces were written for exactly this audience. `assert_cluster_permission` says
    which level the key holds and points at whoami; `assert_explicit_capability` says the
    capability is never inherited and has to be granted deliberately. Replacing those with
    "forbidden" would throw away the only part a caller can act on.
    """
    detail = exc.detail
    if isinstance(detail, dict):
        message = str(detail.get("message") or detail.get("error") or detail)
    else:
        message = str(detail)
    return MCPError(code=INVALID_PARAMS, message=message, data={"http_status": exc.status_code})
