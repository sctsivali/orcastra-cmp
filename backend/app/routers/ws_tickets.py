"""POST /ws-tickets: mint the single-use tickets that open this app's WebSockets.

important: the checks here are advisory. They answer a page early (403 for a socket it cannot
open, the break-glass detail for a tenant console) instead of letting it open a socket that is
refused, but the socket runs its own authorization on connect and that is what decides. A ticket
names its kind and target, so it cannot be spent on any other socket.
"""

import logging
import time
from dataclasses import replace
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Response, status

from app.core.auth import AuthenticatedUser, get_current_user
from app.core.dependencies import require_tenant
from app.core.instance_access import verify_instance_access
from app.core.org_scope import org_scope_dependency, verify_cluster_access
from app.core.ws_ticket import FEED_KINDS, SPICE_CHANNELS, TICKET_TTL_SECONDS, WsTarget, claims_for
from app.core.ws_ticket_store import TicketBudgetExceeded, TicketStoreUnavailable, get_ws_ticket_store
from app.services.migration_fence import MigrationFenceConflict, assert_instance_not_migration_fenced
from app.services.spice_session import new_group
from app.models.ws_ticket_schemas import (
    ConsoleTicketRequest,
    EventsTicketRequest,
    MonitoringTicketRequest,
    TerminalTicketRequest,
    WsTicketRequest,
    WsTicketResponse,
)

logger = logging.getLogger(__name__)

# A feed closes when its session ends, so a ticket minted this close to the end would open a
# socket only to close it again. The 401 makes the page refresh its session and ask once more.
MIN_FEED_SESSION_SECONDS = 30

router = APIRouter(
    prefix="/ws-tickets",
    tags=["WebSocket tickets"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)


def _is_admin(user: Optional[AuthenticatedUser]) -> bool:
    from app.core.rbac import Role, get_highest_role, resolve_roles

    return user is None or get_highest_role(resolve_roles(user)) == Role.ADMIN


async def _session_gate(user: Optional[AuthenticatedUser], host: str) -> None:
    """The break-glass rule a shell or console checks on connect: 403 with its detail."""
    if user is None:
        return
    from app.core.database import async_session_maker
    from app.services.break_glass_service import enforce_sensitive_access

    async with async_session_maker() as db:
        await enforce_sensitive_access(user, host, db)


async def _target(body: WsTicketRequest, user: Optional[AuthenticatedUser]) -> WsTarget:
    """The target the ticket binds, after the checks its socket will repeat on connect."""
    if isinstance(body, MonitoringTicketRequest):
        if not _is_admin(user):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Monitoring live updates are for Admins")
        return WsTarget()
    if isinstance(body, EventsTicketRequest):
        verify_cluster_access(body.host)
        return WsTarget(host=body.host)
    if isinstance(body, (TerminalTicketRequest, ConsoleTicketRequest)):
        if user is not None and not (user.email or "").strip():
            # important: an open shell is re-checked against Authentik by email every minute; one
            # opened without an email could never be, so it is refused here rather than cut later.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This session carries no email address, so a shell or console opened with it could not be re-checked",
            )
        bound_project = verify_instance_access(body.host, body.project, body.instance, "console")
        await _session_gate(user, body.host)
        # note: the socket refuses a fenced instance before accept, where the browser cannot
        # read why. Refusing the ticket here says it in words the page can show.
        try:
            await assert_instance_not_migration_fenced(
                cluster_id=body.host, project=bound_project, instance_name=body.instance,
            )
        except MigrationFenceConflict as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.detail) from exc
        console_type = body.console_type if isinstance(body, ConsoleTicketRequest) else None
        # note: the project as the page sends it, which is what its socket URL will carry.
        return WsTarget(host=body.host, project=body.project, instance=body.instance, console_type=console_type)
    return WsTarget()


@router.post("", response_model=WsTicketResponse, status_code=status.HTTP_201_CREATED)
async def mint_ws_ticket(
    response: Response,
    body: WsTicketRequest = Body(...),
    user: Optional[AuthenticatedUser] = Depends(get_current_user),
) -> WsTicketResponse:
    """Tickets for one socket (one per SPICE channel for a graphical console), 30 seconds each."""
    response.headers["Cache-Control"] = "no-store"
    expires = getattr(user, "session_exp", None)
    if body.kind in FEED_KINDS and expires is not None and expires - time.time() < MIN_FEED_SESSION_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="The session is about to end. Refresh it and ask again.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    target = await _target(body, user)
    spice = isinstance(body, ConsoleTicketRequest) and body.console_type == "vga"
    claims = claims_for(body.kind, target, user)
    if spice:
        claims = replace(claims, group=new_group())
    claims = claims.to_json()
    count = SPICE_CHANNELS if spice else 1
    store = get_ws_ticket_store()
    user_key = user.user_id if user is not None else "anonymous"
    try:
        tickets = [await store.mint(user_key, claims) for _ in range(count)]
    except TicketBudgetExceeded:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many live connections opened in the last minute. Try again shortly.",
            headers={"Retry-After": "60"},
        )
    except TicketStoreUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Live connections cannot be opened right now. Try again shortly.",
        )
    return WsTicketResponse(
        ticket=tickets[0],
        tickets=tickets if count > 1 else None,
        expires_in=TICKET_TTL_SECONDS,
    )
