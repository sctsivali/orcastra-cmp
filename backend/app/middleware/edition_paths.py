"""Refuses the API paths of Full features an edition does not offer, as if they did not exist.

A router included into the app cannot be taken out again (FastAPI keeps it as one opaque entry),
so an edition names the path prefixes instead and this layer answers 404 before any route, auth
dependency or database read runs. Sockets on a blocked path are refused the same way.
"""

from typing import Iterable, Tuple

from starlette.types import ASGIApp, Receive, Scope, Send

_NOT_FOUND = b'{"detail":"Not found"}'


def _normalized(path: str) -> str:
    # Collapse repeated slashes so //api/v1/x cannot slip past a prefix it equals.
    while "//" in path:
        path = path.replace("//", "/")
    return path


class EditionBlockedPaths:
    def __init__(self, app: ASGIApp, prefixes: Iterable[str]) -> None:
        self.app = app
        self.prefixes: Tuple[str, ...] = tuple(prefixes)

    def blocks(self, path: str) -> bool:
        path = _normalized(path).rstrip("/") or "/"
        return any(path == prefix or path.startswith(prefix + "/") for prefix in self.prefixes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or not self.blocks(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await send({
            "type": "http.response.start",
            "status": 404,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(_NOT_FOUND)).encode())],
        })
        await send({"type": "http.response.body", "body": _NOT_FOUND})
