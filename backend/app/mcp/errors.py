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

from fastapi import HTTPException
from mcp.types import CallToolResult, ImageContent, TextContent

from app.core.blocking import BlockingPoolFull
from app.core.error_sanitizer import public_server_detail, reference_for

logger = logging.getLogger(__name__)

__all__ = [
    "desktop_result",
    "error_code_for",
    "refusal_result",
    "tool_error",
    "tool_result",
]

# The code a caller acts on, read from the status the REST route would have answered. Every value
# but "forbidden" is one of PROXY_ERROR_CODES, so a model sees one vocabulary whichever check
# answered. Decided by status and never by detail text: the prose is written for a person and
# changes with the next rewording.
_STATUS_CODES: Dict[int, str] = {
    400: "invalid_request",
    401: "forbidden",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    422: "invalid_request",
    429: "overloaded",
    # The cluster answered, but not with anything usable. Not "down", so not cluster_unavailable.
    502: "operation_failed",
    503: "cluster_unavailable",
    504: "timeout",
}

def error_code_for(exc: HTTPException) -> str:
    """The error_code for an HTTPException raised by a handler a tool dispatched into.

    important: this used to be "forbidden" for every status. A tripped circuit breaker (503), a
    malformed name (400), an instance that does not exist (404) and an ambiguous one (409) all
    read as "this key may not do that", which sends a model to ask an operator for a grant it
    already holds instead of waiting for the cluster, fixing its arguments, or giving up.

    note: a 503 caused by a full worker pool is the server being busy, not the cluster being down,
    so it is `overloaded`, matching what `proxy_operation` already answers for the same cause.
    """
    if exc.status_code == 503 and isinstance(exc.__cause__, BlockingPoolFull):
        return "overloaded"
    code = _STATUS_CODES.get(exc.status_code)
    if code is not None:
        return code
    return "invalid_request" if 400 <= exc.status_code < 500 else "operation_failed"


def refusal_result(exc: HTTPException) -> CallToolResult:
    """An HTTPException from a dispatched handler, as a tool result the model can act on.

    A `Retry-After` the handler set (the desktop budget does) is carried into the text, because a
    tool result has no headers and "overloaded" without a wait is an invitation to retry at once.

    important: a 5xx is shown exactly as REST shows it (`public_server_detail`): a 500 becomes a
    reference, and a 502/503/504 keeps its message with URLs and paths stripped. It went out raw
    before, so a tripped breaker's reason (an exception string that can name a Vault URL or a
    certificate path) reached the model verbatim.
    """
    if exc.status_code >= 500:
        reference, full = reference_for(exc)
        logger.warning("MCP tool refused with %s, reference %s: %s", exc.status_code, reference, full)
        shown = public_server_detail(exc.status_code, exc.detail, reference)
        message = str(shown.get("message", "")) if isinstance(shown, dict) else str(shown)
    else:
        message = str(exc.detail)
    retry_after = (exc.headers or {}).get("Retry-After")
    if retry_after:
        message = f"{message} Retry after {retry_after}s."
    return tool_error(message, error_code=error_code_for(exc))


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


def _promote_image(result: CallToolResult) -> CallToolResult:
    """Move a captured frame out of the JSON text and into a real image content block.

    important: without this a screenshot never reaches the model as an image. The capture came
    back as base64 inside the JSON rendering of the payload, which a client presents as text, so
    the one thing this whole feature exists to do did not happen: the model could not see the
    screen.

    The second cost was the size. A frame in a text block is charged as text, roughly one token
    per four base64 characters, where an image content block of the same frame is charged in
    visual tokens. The 1280x800 target this feature is built around is 1334 visual tokens; the
    same frame as base64 text is thousands, and on a real desktop rather than an empty test one it
    is hundreds of thousands. Every sizing decision in the desktop module assumes the image path.

    The bytes are also dropped from the text and the structured payload rather than left in both,
    because the response carried the whole frame twice. Everything a programmatic caller needs to
    reason about the capture, its media type and its size, stays.
    """
    if result.is_error:
        return result
    payload = result.structured_content or {}
    data = payload.get("data")
    image = data.get("image") if isinstance(data, dict) else None
    encoded = image.get("data") if isinstance(image, dict) else None
    if not encoded:
        return result

    import json

    trimmed = {
        **payload,
        "data": {**data, "image": {k: v for k, v in image.items() if k != "data"}},
    }
    return CallToolResult(
        content=[
            ImageContent(
                type="image", data=encoded, mimeType=image.get("media_type") or "image/png"
            ),
            TextContent(type="text", text=json.dumps(trimmed, indent=2, default=str)),
        ],
        structured_content=trimmed,
        is_error=False,
    )


def desktop_result(result: CallToolResult) -> CallToolResult:
    """A desktop payload, with a failed action rendered as a failure and a frame as an image.

    important: a desktop action reports its own outcome in `data.ok`, separately from whether the
    proxy operation ran. The operation running is what `call_proxy` sees, so a capture that failed
    inside the guest came back with `isError` false and a payload saying `ok: false`. A model
    reads the flag before it reads the payload, so it was told the screenshot succeeded and then
    handed no image. Measured on a wlroots guest whose capture failed for a missing environment
    variable: the tool call was a success and the screen was never seen.
    """
    if result.is_error:
        return result
    payload = result.structured_content or {}
    data = payload.get("data")
    if isinstance(data, dict) and data.get("ok") is False:
        detail = data.get("detail") or data.get("error") or "the desktop action failed"
        action = data.get("action")
        where = f"{action}: " if action else ""
        return tool_error(f"{where}{detail}", error_code=data.get("error_code"))
    return _promote_image(result)
