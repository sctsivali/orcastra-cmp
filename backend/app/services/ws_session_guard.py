"""How long an open WebSocket keeps the authority it was opened with.

important: a socket stays open as long as the browser keeps it, so authorizing once at connect is
not enough. A feed (monitoring, events, tasks) closes with 4401 when its access token expires and
the page reconnects with a fresh ticket. An interactive socket (terminal, console) outlives its
token, so a running command is not killed; every minute it re-checks the account and roles, the
org scope and its own access rule, and closes with 4403 when any says no or the highest role
changed (the ticket's groups are frozen and scope is resolved from them).

An Authentik outage never closes a session (see ws_identity); five failed checks in a row for any
other reason close it with 1011. Close reasons are the tokens of core/ws_close.py.
"""

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

from fastapi import HTTPException, WebSocket

from app.core import ws_close
from app.core.auth import AuthenticatedUser
from app.core.org_scope import set_org_scope_for_ws
from app.core.rbac import get_highest_role, roles_for_group_names
from app.services.ws_identity import check_identity

logger = logging.getLogger(__name__)

SESSION_ENDED = ws_close.AUTH
ACCESS_REVOKED = ws_close.FORBIDDEN
REAUTHORIZE_SECONDS = 60.0
MAX_CONSECUTIVE_ERRORS = 5
COULD_NOT_CHECK = "Could not re-authorize this session"
ACCESS_REVOKED_REASON = "Access revoked"

# Why access ended, as the detail of an access_revoked close.
ACCOUNT_ENDED = "account_ended"
ROLE_CHANGED = "role_changed"
ACCESS_ENDED = "access_ended"

AccessCheck = Callable[[AuthenticatedUser], Awaitable[None]]


async def _close(websocket: WebSocket, code: int, reason: str) -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except Exception:  # noqa: BLE001 - the socket may already be gone
        pass


async def close_when_session_ends(websocket: WebSocket, user: Optional[AuthenticatedUser]) -> None:
    """For a feed: close with 4401 at the session's end, so the page reconnects under a new one.

    With no session to end (auth disabled) it waits until cancelled, because feeds race it
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

    async def refused(reason: str, detail: str) -> None:
        if reason == COULD_NOT_CHECK:
            await _close(websocket, ws_close.SERVER, "reauthorize_failed")
        else:
            await _close(websocket, ACCESS_REVOKED, f"access_revoked:{detail}")

    await _reauthorize(user, check, refused, label=label, interval=interval)


async def reauthorize_until(
    user: Optional[AuthenticatedUser],
    check: AccessCheck,
    refused: Callable[[str], Awaitable[None]],
    *,
    label: str,
    interval: float = REAUTHORIZE_SECONDS,
) -> None:
    """Re-check `user` every `interval` until they lose access, then call `refused(reason)` once.

    A long transfer holds what a shell holds, so it keeps to the same rules: an expired token does
    not end it, an Authentik outage does not end it, and five failed checks in a row do.
    """

    async def refused_without_detail(reason: str, _detail: str) -> None:
        await refused(reason)

    await _reauthorize(user, check, refused_without_detail, label=label, interval=interval)


async def _reauthorize(
    user: Optional[AuthenticatedUser],
    check: AccessCheck,
    refused: Callable[[str, str], Awaitable[None]],
    *,
    label: str,
    interval: float,
) -> None:
    """The loop behind both: `refused(reason, detail)` once, the detail naming why access ended."""
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
                await refused(ACCESS_REVOKED_REASON, ACCOUNT_ENDED)
                return
            if identity.roles is not None and get_highest_role(list(identity.roles)) != opened_as:
                logger.warning("Closing %s: the role of %s changed since it opened", label, user.user_id)
                await refused(ACCESS_REVOKED_REASON, ROLE_CHANGED)
                return
            # Only sessions that drive a guest use this loop: a paid term that ends while one is
            # open closes it like any other change (core/sale_term.py).
            await set_org_scope_for_ws(user, interactive=True)
            await check(user)
            errors = 0
        except HTTPException as exc:
            if exc.status_code in (401, 403, 404):
                logger.warning("Closing %s: access ended for %s (%s)", label, user.user_id, exc.status_code)
                await refused(ACCESS_REVOKED_REASON, ACCESS_ENDED)
                return
            errors += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - counted, closed after five in a row
            errors += 1
            logger.warning("Could not re-authorize %s (%d in a row): %s", label, errors, str(exc)[:200])
        if errors >= MAX_CONSECUTIVE_ERRORS:
            await refused(COULD_NOT_CHECK, "")
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


def file_session_check(host: str, project: str, instance_name: str) -> AccessCheck:
    """The same rule for a guest's files: the file gate, then ownership or a live grant."""

    async def check(user: AuthenticatedUser) -> None:
        from app.core.database import async_session_maker
        from app.core.instance_access import verify_file_access
        from app.services.break_glass_service import enforce_sensitive_access

        verify_file_access(host, project, instance_name)
        async with async_session_maker() as db:
            await enforce_sensitive_access(user, host, db)

    return check
