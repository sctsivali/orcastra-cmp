"""Which channel a failure takes, decided in one place.

MCP has two, and they mean different things to a model. Getting this wrong is not cosmetic: a
403 rendered as a protocol error is invisible to the model as anything but "the server broke", so
it retries blindly instead of reporting something a human can fix.

    transport / protocol failure  ->  HTTP status, or a JSON-RPC error
    authorization or cluster failure the model can act on  ->  isError CallToolResult

The rule of thumb: if the answer is "your credential is wrong", the caller's operator has to fix
it and the model cannot, so it belongs on the protocol channel. If the answer is "this key may not
do that to that cluster" or "the cluster is down", the model should read it, stop, and say so.

There used to be a third helper here rendering an HTTPException as a protocol error, and the
capability gate used it. That put an authorization refusal on the channel this module's own rule
reserves for credential failures, so whether a model could read why it was refused depended on
which check caught it first. A credential failure is now an HTTP status from the transport
endpoint, which is the channel a client acts on, and nothing renders one as a protocol error.
"""

import logging
from typing import Any, Dict, Optional

from mcp.types import CallToolResult, TextContent

logger = logging.getLogger(__name__)

__all__ = [
    "tool_error",
    "tool_result",
]

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
