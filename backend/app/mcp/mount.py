"""Mounting the MCP server inside this FastAPI app, around three hazards in the SDK's defaults.

1. `streamable_http_app()` returns a Starlette app whose lifespan runs the session manager, and
   Starlette does NOT propagate lifespan into a mounted sub-app. Mounted as-is, the manager's task
   group is never created and every request raises "Task group is not initialized". So the app is
   built to materialise the manager, the app itself is discarded, and `main.py`'s own lifespan
   drives `manager.run()`.

2. That call also defaults `host="127.0.0.1"`, which auto-enables DNS-rebinding protection with a
   localhost-only allow-list. Behind a real hostname every request is then rejected opaquely, so
   transport security is passed explicitly instead of inherited.

3. `app.mount()` compiles to a prefix route, so a POST to the canonical path is answered with a
   307 to the trailing-slash form. Both the redirect and the retry pass through
   RequestSecurityMiddleware, which spends two IP rate-limit tokens per JSON-RPC message. A plain
   `Route` with an ASGI endpoint has no such rewrite, and is what the SDK itself does.
"""

import logging
from typing import Any, Dict, Optional, Tuple

from fastapi import HTTPException

from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.core.config import Settings

from .auth import credentials_from_headers
from .scope import SCOPE_API_KEY
from .server import build_mcp_server

logger = logging.getLogger(__name__)

__all__ = ["build_mcp_route"]

# Answered without reaching the session manager. The transport spec explicitly permits 405 for
# both, and refusing them is deliberate rather than lazy: in stateless mode a standalone SSE
# stream is one nothing can ever be written to, so honouring GET would hold one of the server's
# concurrency slots per connected client, forever, for no function.
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
        # note: empty on purpose. Origin is not the credential here. A browser cannot attach
        # X-API-Key-Secret cross-origin without a preflight, and CORSMiddleware already answers
        # preflights only for the configured origins.
        allowed_origins=[],
    )


class _McpEndpoint:
    """The ASGI endpoint, as a callable INSTANCE rather than a function.

    important: Starlette decides how to treat an endpoint with
    `inspect.isfunction(endpoint) or inspect.ismethod(endpoint)` (starlette/routing.py). A plain
    function, including a closure, is wrapped as a request/response handler and its `methods`
    default to GET only, so a JSON-RPC POST is answered 405 and the ASGI three-argument call
    raises TypeError. An instance takes neither branch and is used as raw ASGI with `methods`
    left as None, which is what makes every verb reach this code. Both failures were observed
    before this was a class.
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

        # important: the credential is VERIFIED here, at the transport boundary, rather than
        # inside the message loop. This used to be a presence check only, and the two
        # consequences were both live:
        #
        # 1. A wrong, revoked, expired or deleted key was answered HTTP 200 carrying a JSON-RPC
        #    error. A client cannot tell that from a tool that failed, so it retries a dead
        #    credential instead of telling the operator to fix it, and revoking a key stopped
        #    being an unambiguous signal. The authorization spec calls for 401 here, and this
        #    deployment's own documentation already claimed it.
        # 2. `initialize` and `ping` were exempt from authentication entirely, so anyone who
        #    could reach the port received the server instructions and the full capability
        #    advertisement without a valid key, on a path that consumed no per-key budget.
        #
        # A credential failure is an HTTP status; an AUTHORIZATION failure stays a tool result,
        # because that is the one a model can act on. That split is the whole reason this is
        # here and the capability gate is not.
        try:
            api_key = await self._authenticate(key_id, secret)
        except HTTPException as exc:
            # note: the key id is logged, never the secret, matching the REST surface.
            logger.warning("MCP authentication failed for key %s: %s", key_id, exc.status_code)
            await self._refuse(
                scope, receive, send, exc.status_code, exc.detail, dict(exc.headers or {})
            )
            return
        except Exception:  # noqa: BLE001 - see below
            # important: a DECISION about the credential arrives as HTTPException and is handled
            # above. Anything else here is the authentication backend failing rather than the
            # caller being wrong, and it must not leave this endpoint as a bare 500.
            #
            # This path grew a dependency it did not used to have: `validate_api_key` now consults
            # the extensions installation row for a credential issued by an add-on install, so a
            # database outage reaches authentication where previously only Vault did.
            # `validate_api_key` already answers 503 for the analogous Vault timeout, so matching
            # it keeps this endpoint's contract true, which is that a credential failure is always
            # a clean HTTP status a client can act on.
            logger.exception("MCP authentication errored for key %s", key_id)
            await self._refuse(
                scope, receive, send, 503, "Authentication backend unavailable"
            )
            return

        # Read back by ApiKeyMiddleware. The SDK builds its Request from this very scope
        # (`streamable_http.handle_request`), so this is the message loop's view of the same
        # dictionary, and the key is validated exactly once per HTTP request. Spending the
        # per-key budget twice would silently halve every integration's allowance.
        scope[SCOPE_API_KEY] = api_key

        await self._manager.handle_request(scope, receive, send)

    @staticmethod
    async def _authenticate(key_id: str, secret: str) -> Any:
        # Imported here because `app.routers.integrations` imports this package's server at
        # module scope, and at build time that circle is not yet closed.
        from app.routers.integrations import validate_api_key

        return await validate_api_key(x_api_key_id=key_id, x_api_key_secret=secret)

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


def build_mcp_route(settings: Settings) -> Tuple[Route, Any]:
    """The route to append, and the session manager `main.py`'s lifespan must run."""
    mcp = build_mcp_server(settings)
    path = f"{settings.api_prefix}/mcp"

    # note: the returned app is deliberately discarded. Calling this is what constructs and caches
    # the session manager; `MCPServer.session_manager` raises unless it has run. So this is not
    # dead code, it is the constructor.
    mcp.streamable_http_app(
        streamable_http_path=path,
        stateless_http=True,
        json_response=True,
        transport_security=_transport_security(settings),
    )
    manager = mcp.session_manager

    # methods=None so the route matches every verb and the 405s above are ours to word, rather
    # than Starlette answering with a bare one that says nothing.
    return Route(path, endpoint=_McpEndpoint(manager), methods=None), manager
