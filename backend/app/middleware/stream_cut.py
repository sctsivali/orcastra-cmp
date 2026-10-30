"""Keeps a cut download from looking like a finished one.

important: this has to be outermost, and it has to exist. `BaseHTTPMiddleware` sends the terminal
chunk when the inner app stops and re-raises only afterwards, so with four of them in this stack a
chunked archive whose guest died ended as a normal response. The bytes were a truncated gzip, and a
browser reported a successful download of a corrupt file.

A stream that knows it was cut marks its scope, this layer swallows the clean end, and the exception
that follows makes uvicorn close the connection with no terminal chunk, which is what every HTTP
client reads as a failed transfer. A sized response is protected by its Content-Length already.
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
        # note: a marked stream that somehow returned must still not end cleanly, or the server
        # would be told nothing and hold the connection open for its keep-alive.
        if scope.get(_CUT):
            raise ConnectionAbortedError("a marked stream was cut")
