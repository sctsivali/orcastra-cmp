"""Mounting the MCP server inside this FastAPI app, around three hazards in the SDK's defaults.

1. Starlette does not run a mounted sub-app's lifespan, so the session manager behind
   `streamable_http_app()` would never create its task group ("Task group is not initialized").
   The app is built only to materialise the manager and then discarded; `main.py`'s lifespan
   drives `manager.run()`.
2. That call defaults `host="127.0.0.1"`, which enables DNS-rebinding protection with a
   localhost-only allow-list and rejects every request behind a real hostname, so transport
   security is passed explicitly.
3. `app.mount()` is a prefix route that answers a POST to the canonical path with a 307 to the
   trailing slash; RequestSecurityMiddleware then spends two IP rate-limit tokens per JSON-RPC
   message. A plain `Route` with an ASGI endpoint, as the SDK uses, has no such rewrite.
"""

import logging
from typing import Any, Dict, Optional, Tuple

from fastapi import HTTPException

from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from app.core.config import Settings

from .auth import credentials_from_headers
from .limits import request_body_limit
from .scope import SCOPE_API_KEY
from .server import build_mcp_server

logger = logging.getLogger(__name__)

__all__ = ["build_mcp_route"]

# Answered without reaching the session manager; the transport spec permits 405 for both. In
# stateless mode nothing can ever be written to a standalone SSE stream, so honouring GET would
# hold one of the server's concurrency slots per connected client, indefinitely.
_UNSUPPORTED = {
    "GET": "this server does not open standalone SSE streams; POST JSON-RPC instead",
    "DELETE": "this server is stateless and has no session to terminate",
}


def _transport_security(settings: Settings) -> TransportSecuritySettings:
    allowed = [
        host.strip()
        for host in (getattr(settings, "mcp_allowed_hosts", "") or "").split(",")
        if host.strip()
    ]
    return TransportSecuritySettings(
        # Off when no hosts are configured, which is correct behind a proxy that already pins
        # Host. On when they are, and then it is the operator's list rather than localhost.
        enable_dns_rebinding_protection=bool(allowed),
        allowed_hosts=allowed,
        # Left empty: Origin is not the credential here. A browser cannot attach
        # X-API-Key-Secret cross-origin without a preflight, and CORSMiddleware answers
        # preflights only for the configured origins.
        allowed_origins=[],
    )


class _McpEndpoint:
    """The ASGI endpoint, as a callable instance rather than a function.

    important: Starlette decides how to treat an endpoint with
    `inspect.isfunction(endpoint) or inspect.ismethod(endpoint)` (starlette/routing.py). A plain
    function, including a closure, is wrapped as a request/response handler and its `methods`
    default to GET only, so a JSON-RPC POST is answered 405 and the ASGI three-argument call
    raises TypeError. An instance takes neither branch and is used as raw ASGI with `methods`
    left as None, which is what makes every verb reach this code.
    """

    def __init__(self, manager: Any) -> None:
        self._manager = manager

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        method = scope.get("method", "")
        if method in _UNSUPPORTED:
            await JSONResponse(
                {"detail": _UNSUPPORTED[method]}, status_code=405
            )(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        key_id, secret = credentials_from_headers(headers)
        if not key_id or not secret:
            await self._refuse(
                scope,
                receive,
                send,
                401,
                (
                    "missing API key. Send X-API-Key-ID and X-API-Key-Secret headers, or "
                    "Authorization: Bearer <key-id>:<key-secret>."
                ),
            )
            return

        # important: the credential is verified here, at the transport boundary, rather than
        # inside the message loop, for every method:
        #
        # 1. A wrong, revoked, expired or deleted key gets the HTTP status the authorization spec
        #    calls for. An HTTP 200 carrying a JSON-RPC error looks to a client like a tool that
        #    failed, so it would retry a dead credential instead of telling the operator.
        # 2. `initialize` and `ping` are authenticated too, so the server instructions and the
        #    capability advertisement need a valid key and spend per-key budget.
        #
        # A credential failure is an HTTP status; an authorization failure stays a tool result,
        # because that is the one a model can act on. That split is why this is here and the
        # capability gate is not.
        budget = Response()
        try:
            api_key = await self._authenticate(key_id, secret, budget)
        except HTTPException as exc:
            # The key id is logged, never the secret, matching the REST surface.
            logger.warning("MCP authentication failed for key %s: %s", key_id, exc.status_code)
            await self._refuse(
                scope, receive, send, exc.status_code, exc.detail, dict(exc.headers or {})
            )
            return
        except Exception:  # noqa: BLE001 - see below
            # important: a decision about the credential arrives as HTTPException and is handled
            # above. Anything else here is the authentication backend failing (Vault, or the
            # database `validate_api_key` reads for a credential an add-on install issued),
            # not the caller being wrong. It is answered 503, as `validate_api_key` answers a
            # Vault timeout, so a credential failure is always a clean HTTP status, never a 500.
            logger.exception("MCP authentication errored for key %s", key_id)
            await self._refuse(
                scope, receive, send, 503, "Authentication backend unavailable"
            )
            return

        # Read back by ApiKeyMiddleware. The SDK builds its Request from this very scope
        # (`streamable_http.handle_request`), so this is the message loop's view of the same
        # dictionary, and the key is validated exactly once per HTTP request. Validating twice
        # would spend the per-key budget twice.
        scope[SCOPE_API_KEY] = api_key

        await self._manager.handle_request(scope, receive, _with_budget_headers(send, budget))

    @staticmethod
    async def _authenticate(key_id: str, secret: str, budget: Response) -> Any:
        # Imported here because `app.routers.integrations` imports this package's server at
        # module scope, and at build time that circle is not yet closed.
        from app.routers.integrations import validate_api_key

        return await validate_api_key(
            x_api_key_id=key_id, x_api_key_secret=secret, response=budget
        )

    @staticmethod
    async def _refuse(
        scope: Any,
        receive: Any,
        send: Any,
        status: int,
        detail: Any,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        out = dict(headers or {})
        if status == 401:
            out.setdefault("WWW-Authenticate", 'Bearer realm="orcastra-mcp"')
        await JSONResponse({"detail": detail}, status_code=status, headers=out)(
            scope, receive, send
        )


def _with_budget_headers(send: Any, budget: Response) -> Any:
    """`send`, adding the per-key budget headers `validate_api_key` computed to the response.

    Matches the REST surface, which carries `X-RateLimit-*` on every answer, so an agent can see
    what is left of the budget this endpoint spends before it hits the ceiling. Absent when the
    limiter is off or its store could not answer, as on REST.
    """
    extra = [
        (name.encode("latin-1"), value.encode("latin-1"))
        for name, value in budget.headers.items()
        if name.lower().startswith("x-ratelimit-")
    ]
    if not extra:
        return send

    async def _send(message: Dict[str, Any]) -> None:
        if message.get("type") == "http.response.start":
            message = {**message, "headers": [*message.get("headers", []), *extra]}
        await send(message)

    return _send


def build_mcp_route(settings: Settings) -> Tuple[Route, Any]:
    """The route to append, and the session manager `main.py`'s lifespan must run."""
    mcp = build_mcp_server(settings)
    path = f"{settings.api_prefix}/mcp"

    # The returned app is not used: this call constructs and caches the session manager, and
    # `MCPServer.session_manager` raises unless it has run.
    mcp.streamable_http_app(
        streamable_http_path=path,
        stateless_http=True,
        json_response=True,
        transport_security=_transport_security(settings),
        max_request_body_size=request_body_limit(),
    )
    manager = mcp.session_manager

    # methods=None so the route matches every verb and the 405s above are ours to word, rather
    # than Starlette answering with a bare one that says nothing.
    return Route(path, endpoint=_McpEndpoint(manager), methods=None), manager
