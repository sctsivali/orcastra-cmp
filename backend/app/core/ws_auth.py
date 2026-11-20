"""Who opened a WebSocket: a redeemed ticket, or a ?token= while `ws_query_token_enabled` allows it.

important: a ticket is checked against the socket it opens. It must be of this socket's kind,
for this socket's Cluster, project, instance and console type, and minted under a session that
has not ended; anything else is refused, and the ticket is spent either way. A socket that
presents both a ticket and a token while tokens are refused is refused too, so a URL that still
carries a token never opens a socket.

Returns None to refuse, like get_ws_user, and raises only for a server that cannot verify anyone
(auth enabled without an issuer). A refusal also records its close code and reason token on the
socket (refusal_of), so an interactive socket can tell the page why (core/ws_close.py).
"""

import logging
import time
from typing import Awaitable, Callable, Optional

from fastapi import Depends, WebSocket, WebSocketException, status

from app.core.auth import AuthenticatedUser, admit_socket, get_ws_user, token_verification_configured
from app.core import ws_close
from app.core.config import Settings, get_settings
from app.core.rbac import session_has_role
from app.core.ws_ticket import TICKET_TTL_SECONDS, TicketClaims, socket_target
from app.core.ws_ticket_store import get_ws_ticket_store

logger = logging.getLogger(__name__)

# Redis expires a ticket at its TTL; this bounds a signed entry written back after it was spent.
_TICKET_CLOCK_SKEW_SECONDS = 5.0


def _refuse_as(websocket: WebSocket, code: int, reason: str) -> None:
    """Remember why this socket is refused, for the close the endpoint sends."""
    state = getattr(websocket, "state", None)
    if state is None:
        return
    try:
        state.ws_refusal = (code, reason)
    except AttributeError:  # a stand-in socket whose state takes no attributes
        pass


def refusal_of(websocket: WebSocket) -> tuple[int, str]:
    """The close code and reason token ws_user recorded; auth_required when it recorded none."""
    recorded = getattr(getattr(websocket, "state", None), "ws_refusal", None)
    if isinstance(recorded, tuple) and len(recorded) == 2:
        return recorded
    return ws_close.AUTH, "auth_required"


async def _redeemed(kind: str, websocket: WebSocket, ticket: str) -> Optional[AuthenticatedUser]:
    raw = await get_ws_ticket_store().redeem(ticket)
    if raw is None:
        logger.warning("WebSocket refused: unknown, spent or expired ticket for a %s socket", kind)
        _refuse_as(websocket, ws_close.AUTH, "ticket_invalid")
        return None
    try:
        claims = TicketClaims.from_json(raw)
    except (KeyError, TypeError, ValueError):
        logger.warning("WebSocket refused: unreadable ticket for a %s socket", kind)
        _refuse_as(websocket, ws_close.AUTH, "ticket_invalid")
        return None
    if claims.minted_at + TICKET_TTL_SECONDS + _TICKET_CLOCK_SKEW_SECONDS < time.time():
        logger.warning("WebSocket refused: a %s ticket minted more than %d s ago", kind, TICKET_TTL_SECONDS)
        _refuse_as(websocket, ws_close.AUTH, "ticket_invalid")
        return None
    if claims.kind != kind:
        logger.warning("WebSocket refused: a %s ticket presented to a %s socket", claims.kind, kind)
        _refuse_as(websocket, ws_close.AUTH, "ticket_invalid")
        return None
    if claims.target != socket_target(kind, websocket.path_params, websocket.query_params):
        logger.warning("WebSocket refused: a %s ticket presented for another target", kind)
        _refuse_as(websocket, ws_close.AUTH, "ticket_invalid")
        return None
    # The same clock skew the access token was checked with when the ticket was minted.
    leeway = getattr(get_settings(), "auth_jwt_leeway_seconds", 0)
    if claims.session_exp is not None and claims.session_exp + leeway <= time.time():
        logger.warning("WebSocket refused: the %s ticket's session has ended", kind)
        _refuse_as(websocket, ws_close.AUTH, "session_ended")
        return None
    # The display group is kept on the socket rather than the identity: the console endpoint reads
    # it to join the channels of one SPICE display to one LXD operation.
    websocket.state.ws_ticket_group = claims.group
    return claims.user()


def ws_user(kind: str) -> Callable[..., Awaitable[Optional[AuthenticatedUser]]]:
    """The dependency that identifies the caller of a socket of `kind`."""

    async def dependency(
        websocket: WebSocket, settings: Settings = Depends(get_settings)
    ) -> Optional[AuthenticatedUser]:
        if not settings.auth_enabled:
            return None
        if not token_verification_configured(settings):
            logger.error(
                "AUTH_ENABLED=true but AUTHENTIK_ISSUER is not configured; refusing every WebSocket "
                "until it is set, rather than serving them unauthenticated"
            )
            raise WebSocketException(
                code=status.WS_1011_INTERNAL_ERROR,
                reason="Authentication is enabled but not configured on this server",
            )
        ticket = websocket.query_params.get("ticket")
        presented_token = "token" in websocket.query_params
        if ticket:
            user = await _redeemed(kind, websocket, ticket)
            refusal = await admit_socket(user, websocket) if user is not None else None
            if refusal is not None:
                logger.warning("WebSocket refused: a %s ticket's holder was not admitted (%s)", kind, refusal)
                _refuse_as(websocket, ws_close.AUTH, refusal)
                return None
            if presented_token and not settings.ws_query_token_enabled:
                logger.warning("WebSocket refused: a %s socket presented a token beside its ticket", kind)
                _refuse_as(websocket, ws_close.AUTH, "token_refused")
                return None
            return None if await _refused_without_role(kind, user, websocket) else user
        if presented_token:
            if not settings.ws_query_token_enabled:
                logger.warning("WebSocket refused: a %s socket presented ?token=, which is no longer accepted", kind)
                _refuse_as(websocket, ws_close.AUTH, "token_refused")
                return None
            user = await get_ws_user(websocket, settings)
            if user is None:
                _refuse_as(websocket, ws_close.AUTH, "token_refused")
            return None if await _refused_without_role(kind, user, websocket) else user
        logger.warning("WebSocket refused: a %s socket presented no ticket", kind)
        _refuse_as(websocket, ws_close.AUTH, "auth_required")
        return None

    dependency.__name__ = f"ws_user_{kind}"
    return dependency


async def _refused_without_role(kind: str, user: Optional[AuthenticatedUser], websocket: WebSocket) -> bool:
    """Whether a caller identified for a socket has no Orcastra role and must be refused.

    important: the REST routes refuse such a caller (rbac.refuse_unmapped); a socket that let one in
    would hand it the live feeds the access notice is meant to keep from it. A groupless member of
    an active organization is a Tenant there and here (rbac.session_has_role).
    """
    if user is None or await session_has_role(user):
        return False
    logger.warning("WebSocket refused: a %s socket opened by a caller with no role", kind)
    _refuse_as(websocket, ws_close.FORBIDDEN, "forbidden")
    return True
