"""How an interactive socket (terminal, console) is refused or closed for a reason of access.

important: a socket closed before accept() never reaches the browser as a close frame. uvicorn
answers the upgrade with HTTP 403, the browser reports 1006 with no reason, and the page cannot
tell an expired ticket from a dropped network, so nothing recovers. Every refusal here accepts
first and then closes with a code and a reason token the page acts on (frontend/src/lib/ws-close.ts
holds the same table; scripts/checks/check-ws-close-contract.mjs keeps the two equal).

- 4401 AUTH: the caller could not be identified. auth_required (no ticket), ticket_invalid
  (unknown, spent, expired, unreadable or minted for another socket), session_ended (minted under
  a session that has ended), token_refused (an access token in the URL). The page refreshes its
  session and mints a new ticket, once.
- 4403 FORBIDDEN: forbidden at connect; access_revoked:<detail> while open, with the detail one of
  account_ended, role_changed, access_ended. Final.
- 4428 BREAK_GLASS: break_glass_required at connect, break_glass_expired while open. Final.
- 4423 FENCED: migration_fenced. Final until the move finishes.
- 1011 SERVER: reauthorize_failed, upstream_unavailable, session_failed. Reconnecting may work.
- 1008 POLICY: unknown_console_type, a request no page sends.

note: break-glass is 4428, not 4409: interactive_relay.py already ends a display another viewer
took over with 4409, and the console treats that as "taken over".

The relay's own endings (1000, 1011, 1012, 4404, 4409 in services/interactive_relay.py) keep their
sentences as reasons; the page prints those.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

AUTH = 4401
FORBIDDEN = 4403
BREAK_GLASS = 4428
FENCED = 4423
SERVER = 1011
POLICY = 1008

# Every reason a refusal or an access close may carry, by code. The contract check reads this.
REASONS: dict[int, tuple[str, ...]] = {
    AUTH: ("auth_required", "ticket_invalid", "session_ended", "token_refused"),
    FORBIDDEN: (
        "forbidden",
        "access_revoked:account_ended",
        "access_revoked:role_changed",
        "access_revoked:access_ended",
    ),
    BREAK_GLASS: ("break_glass_required", "break_glass_expired"),
    FENCED: ("migration_fenced",),
    SERVER: ("reauthorize_failed", "upstream_unavailable", "session_failed"),
    POLICY: ("unknown_console_type",),
}

MAX_REASON_BYTES = 64
_TOKEN = re.compile(r"[^a-z0-9_:.-]")


def reason_token(reason: str) -> str:
    """``reason`` as a close-frame token: lowercase ASCII, at most 64 bytes."""
    return _TOKEN.sub("_", (reason or "").strip().lower())[:MAX_REASON_BYTES]


def _still_connecting(websocket: Any) -> bool:
    from starlette.websockets import WebSocketState

    state = getattr(websocket, "application_state", None)
    # A stand-in without Starlette's state (a test double) is treated as not yet accepted.
    return state is None or state == WebSocketState.CONNECTING


async def refuse(websocket: Any, code: int, reason: str) -> None:
    """Close ``websocket`` with ``code`` and a reason token, accepting it first if it is connecting.

    spice-html5 offers the "binary" subprotocol and fails a socket that does not echo it, and a
    client that offered none fails one that names any, so the accept echoes exactly what was asked.
    Never raises: a client that already left needs nothing more.
    """
    token = reason_token(reason)
    try:
        if _still_connecting(websocket):
            scope = getattr(websocket, "scope", None) or {}
            offered = scope.get("subprotocols") or []
            await websocket.accept(subprotocol="binary" if "binary" in offered else None)
        await websocket.close(code=code, reason=token)
    except Exception as exc:  # noqa: BLE001 - the client is gone; nothing to tell it
        logger.debug("Could not deliver close %s (%s): %s", code, token, type(exc).__name__)
