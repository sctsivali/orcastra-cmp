"""Keeps a cut download from looking like a finished one.

important: this must be the outermost middleware. `BaseHTTPMiddleware` sends the terminal chunk
when the inner app stops and re-raises only afterwards, so without this layer a chunked archive cut
mid-stream (ex; its guest died) ends as a normal response and the browser saves a truncated file as
a successful download.

A stream that knows it was cut marks its scope, this layer swallows the clean end, and the exception
that follows makes uvicorn close the connection with no terminal chunk, which every HTTP client
reads as a failed transfer. A sized response is already protected by its Content-Length.
"""

from typing import Any, MutableMapping

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_CUT = "orcastra.stream_cut"


def mark_cut(scope: MutableMapping[str, Any]) -> None:
    scope[_CUT] = True


class StreamCutMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def guarded_send(message: Message) -> None:
            if message["type"] == "http.response.body" and scope.get(_CUT):
                if not message.get("more_body", False):
                    return
            await send(message)

        await self.app(scope, receive, guarded_send)
        # A marked stream that returned must still not end cleanly, or the server is told nothing
        # and holds the connection open for keep-alive.
        if scope.get(_CUT):
            raise ConnectionAbortedError("a marked stream was cut")
