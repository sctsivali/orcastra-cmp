"""Reads the console's X-Byte-Units header so size text the server writes follows the reader.

A plain ASGI middleware rather than BaseHTTPMiddleware: it only sets a context variable around the
call, so it adds no task and never buffers a streamed body.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.units import BYTE_UNITS_HEADER, byte_units_from_header, reset_byte_units, use_byte_units

_HEADER = BYTE_UNITS_HEADER.encode("latin-1")


class ByteUnitsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        raw = next((value for name, value in scope.get("headers") or () if name == _HEADER), None)
        token = use_byte_units(byte_units_from_header(raw.decode("latin-1") if raw is not None else None))
        try:
            await self.app(scope, receive, send)
        finally:
            reset_byte_units(token)
