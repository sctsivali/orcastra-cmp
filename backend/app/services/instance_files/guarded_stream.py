"""A download that stops when it should, and never looks finished when it did not finish.

`StreamingResponse` is the wrong tool here for three reasons. It has no send timeout, so a client
that stops reading parks the handler forever, holding a guest connection and a transfer slot. It
has no way to end a stream when access is withdrawn. And an HTTP response only reads as failed if
it ends without its terminal chunk.

important: this app's middleware is all `BaseHTTPMiddleware`, which finishes a response itself when
the inner app stops, even when it stops by raising. So a cut stream marks its scope for
`middleware/stream_cut.py`, which withholds the terminal chunk, and then raises, so that uvicorn
closes the connection. Without both, a truncated archive reads as a finished download.
"""

import logging
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Tuple

import anyio

from app.core.auth import AuthenticatedUser
from app.services.ws_session_guard import AccessCheck, reauthorize_until

logger = logging.getLogger(__name__)

STALL_SECONDS = 60.0
MAX_STREAM_SECONDS = 6 * 3600.0


class StreamCut(Exception):
    """The transfer did not complete. `reason` goes to the audit record."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ClientLeft(Exception):
    """The browser went away, which is a normal end and needs no error."""


async def pump_guarded(
    receive: Callable[[], Awaitable[Dict[str, Any]]],
    send: Callable[[Dict[str, Any]], Awaitable[None]],
    *,
    status: int,
    headers: List[Tuple[bytes, bytes]],
    body: AsyncIterator[bytes],
    expected: Optional[int],
    user: Optional[AuthenticatedUser],
    check: AccessCheck,
    label: str,
    interval: float,
    on_bytes: Callable[[int], None],
    stall_seconds: float = STALL_SECONDS,
    max_seconds: float = MAX_STREAM_SECONDS,
) -> None:
    """Send the response, racing the body against disconnect, stall, the deadline and revocation."""
    sent = 0
    failure: Optional[BaseException] = None

    async def pump(scope: anyio.CancelScope) -> None:
        nonlocal sent
        with anyio.fail_after(stall_seconds):
            await send({"type": "http.response.start", "status": status, "headers": headers})
        async for piece in body:
            with anyio.fail_after(stall_seconds):
                await send({"type": "http.response.body", "body": piece, "more_body": True})
            sent += len(piece)
            on_bytes(len(piece))
        if expected is not None and sent != expected:
            raise StreamCut("guest file ended early")
        with anyio.fail_after(stall_seconds):
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        scope.cancel()

    async def watch_client(scope: anyio.CancelScope) -> None:
        nonlocal failure
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                failure = ClientLeft()
                scope.cancel()
                return

    async def watch_access(scope: anyio.CancelScope) -> None:
        nonlocal failure

        async def refused(reason: str) -> None:
            nonlocal failure
            failure = StreamCut(reason)
            scope.cancel()

        await reauthorize_until(user, check, refused, label=label, interval=interval)

    async def run_all() -> None:
        async with anyio.create_task_group() as group:
            group.start_soon(pump, group.cancel_scope)
            group.start_soon(watch_client, group.cancel_scope)
            if user is not None:
                group.start_soon(watch_access, group.cancel_scope)

    try:
        with anyio.fail_after(max_seconds):
            await run_all()
    except TimeoutError:
        raise StreamCut("transfer stalled or passed its time limit") from None
    except BaseExceptionGroup as group:
        first = group.exceptions[0]
        if isinstance(first, TimeoutError):
            raise StreamCut("transfer stalled or passed its time limit") from None
        raise first from None
    if failure is not None:
        raise failure
