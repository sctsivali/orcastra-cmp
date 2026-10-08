"""Who opened a WebSocket: a redeemed ticket, or the old ?token= while it is still accepted.

important: a ticket is checked against the socket it opens. It must be of this socket's kind,
for this socket's Cluster, project, instance and console type, and minted under a session that
has not ended; anything else is refused, and the ticket is spent either way. A socket that
presents both a ticket and a token while tokens are refused is refused too, so a URL that still
carries a token never opens a socket.

note: returns None to refuse, like get_ws_user, and raises only for a server that cannot verify
anyone (auth enabled without an issuer). Callers close with 1008 on None.
"""

import logging
import time
from typing import Awaitable, Callable, Optional

from fastapi import Depends, WebSocket, WebSocketException, status

from app.core.auth import AuthenticatedUser, get_ws_user
from app.core.config import Settings, get_settings
from app.core.ws_ticket import TICKET_TTL_SECONDS, TicketClaims, socket_target
from app.core.ws_ticket_store import get_ws_ticket_store

logger = logging.getLogger(__name__)

# Redis expires a ticket at its TTL; this bounds a signed entry written back after it was spent.
_TICKET_CLOCK_SKEW_SECONDS = 5.0


async def _redeemed(kind: str, websocket: WebSocket, ticket: str) -> Optional[AuthenticatedUser]:
    raw = await get_ws_ticket_store().redeem(ticket)
    if raw is None:
        logger.warning("WebSocket refused: unknown, spent or expired ticket for a %s socket", kind)
        return None
    try:
        claims = TicketClaims.from_json(raw)
    except (KeyError, TypeError, ValueError):
        logger.warning("WebSocket refused: unreadable ticket for a %s socket", kind)
        return None
    if claims.minted_at + TICKET_TTL_SECONDS + _TICKET_CLOCK_SKEW_SECONDS < time.time():
        logger.warning("WebSocket refused: a %s ticket minted more than %d s ago", kind, TICKET_TTL_SECONDS)
        return None
    if claims.kind != kind:
        logger.warning("WebSocket refused: a %s ticket presented to a %s socket", claims.kind, kind)
        return None
    if claims.target != socket_target(kind, websocket.path_params, websocket.query_params):
        logger.warning("WebSocket refused: a %s ticket presented for another target", kind)
        return None
    if claims.session_exp is not None and claims.session_exp <= time.time():
        logger.warning("WebSocket refused: the %s ticket's session has ended", kind)
        return None
    return claims.user()


def ws_user(kind: str) -> Callable[..., Awaitable[Optional[AuthenticatedUser]]]:
    """The dependency that identifies the caller of a socket of `kind`."""

    async def dependency(
        websocket: WebSocket, settings: Settings = Depends(get_settings)
    ) -> Optional[AuthenticatedUser]:
        if not settings.auth_enabled:
            return None
        if not settings.authentik_issuer:
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
            if presented_token and not settings.ws_query_token_enabled:
                logger.warning("WebSocket refused: a %s socket presented a token beside its ticket", kind)
                return None
            return user
        if presented_token:
            if not settings.ws_query_token_enabled:
                logger.warning("WebSocket refused: a %s socket presented ?token=, which is no longer accepted", kind)
                return None
            return await get_ws_user(websocket, settings)
        logger.warning("WebSocket refused: a %s socket presented no ticket", kind)
        return None

    dependency.__name__ = f"ws_user_{kind}"
    return dependency
