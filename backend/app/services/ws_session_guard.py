"""How long an open WebSocket keeps the authority it was opened with.

important: authorization used to run once, at connect, and a socket then stayed open for as
long as the browser kept it, whatever happened to the session behind it. Two rules now:

  * a feed (monitoring, events, tasks) closes with 4401 when the access token it was opened
    under expires; the page mints a fresh ticket and reconnects, so the feed is never older
    than the session that is reading it;
  * an interactive socket (terminal, console) is not cut when its token expires, because that
    would kill a running command. Every minute it re-checks the account and its roles, the
    caller's organization scope and its own access rule, and closes with 4403 when any of them
    says no. A caller whose highest role changed is closed too, since the ticket's groups are
    frozen and each round resolves scope from them.

note: an Authentik outage never closes a session (see ws_identity); five checks in a row that
fail for any other reason close it with 1011, since a guard that cannot check is not guarding.
"""

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

from fastapi import HTTPException, WebSocket

from app.core.auth import AuthenticatedUser
from app.core.org_scope import set_org_scope_for_ws
from app.core.rbac import get_highest_role, roles_for_group_names
from app.services.ws_identity import check_identity

logger = logging.getLogger(__name__)

SESSION_ENDED = 4401
ACCESS_REVOKED = 4403
REAUTHORIZE_SECONDS = 60.0
MAX_CONSECUTIVE_ERRORS = 5

AccessCheck = Callable[[AuthenticatedUser], Awaitable[None]]


async def _close(websocket: WebSocket, code: int, reason: str) -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except Exception:  # noqa: BLE001 - the socket may already be gone
        pass


async def close_when_session_ends(websocket: WebSocket, user: Optional[AuthenticatedUser]) -> None:
    """For a feed: close with 4401 at the session's end, so the page reconnects under a new one.

    note: with no session to end (auth disabled) it waits until cancelled, because feeds race it
    against their own loops and a guard that returned at once would end the feed.
    """
    expires = getattr(user, "session_exp", None)
    if expires is None:
        await asyncio.Event().wait()
        return
    await asyncio.sleep(max(0.0, float(expires) - time.time()))
    logger.info("Closing a feed socket: its session ended (user=%s)", getattr(user, "user_id", "?"))
    await _close(websocket, SESSION_ENDED, "Session ended")


async def reauthorize_while_open(
    websocket: WebSocket,
    user: Optional[AuthenticatedUser],
    check: AccessCheck,
    *,
    label: str,
    interval: float = REAUTHORIZE_SECONDS,
) -> None:
    """For an interactive socket: re-check the account, scope and access every `interval`."""
    if user is None:
        return
    opened_as = get_highest_role(roles_for_group_names(list(user.groups)))
    errors = 0
    while True:
        await asyncio.sleep(interval)
        try:
            identity = await check_identity(user)
            if identity.ended:
                logger.warning("Closing %s: the account %s (%s)", label, user.user_id, identity.reason)
                await _close(websocket, ACCESS_REVOKED, "Access revoked")
                return
            if identity.roles is not None and get_highest_role(list(identity.roles)) != opened_as:
                logger.warning("Closing %s: the role of %s changed since it opened", label, user.user_id)
                await _close(websocket, ACCESS_REVOKED, "Access revoked")
                return
            await set_org_scope_for_ws(user)
            await check(user)
            errors = 0
        except HTTPException as exc:
            if exc.status_code in (401, 403, 404):
                logger.warning("Closing %s: access ended for %s (%s)", label, user.user_id, exc.status_code)
                await _close(websocket, ACCESS_REVOKED, "Access revoked")
                return
            errors += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - counted, closed after five in a row
            errors += 1
            logger.warning("Could not re-authorize %s (%d in a row): %s", label, errors, str(exc)[:200])
        if errors >= MAX_CONSECUTIVE_ERRORS:
            await _close(websocket, 1011, "Could not re-authorize this session")
            return


def instance_session_check(host: str, project: str, instance_name: str) -> AccessCheck:
    """A shell's or console's own rule: the instance gate, then ownership or a live break-glass grant."""

    async def check(user: AuthenticatedUser) -> None:
        from app.core.database import async_session_maker
        from app.core.instance_access import verify_instance_access
        from app.services.break_glass_service import enforce_sensitive_access

        verify_instance_access(host, project, instance_name, "console")
        async with async_session_maker() as db:
            await enforce_sensitive_access(user, host, db)

    return check
