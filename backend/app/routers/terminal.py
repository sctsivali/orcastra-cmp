"""Terminal WebSocket: an interactive shell in an LXD instance, proxied through the backend."""

import asyncio
import logging
from typing import Optional
from fastapi import APIRouter, WebSocket, Query, Depends

from app.services.terminal_proxy import TerminalProxy
from app.services.activity_logger import get_activity_logger
from app.core.dependencies import AuthenticatedUser
from app.core.instance_access import verify_instance_access
from app.core import ws_close
from app.core.ws_auth import refusal_of, ws_user
from app.services.ws_session_guard import instance_session_check, reauthorize_while_open
from app.core.org_scope import set_org_scope_for_ws
from app.services.migration_fence import (
    MigrationFenceConflict,
    assert_instance_not_migration_fenced,
)

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

router = APIRouter(
    prefix="/terminal",
    tags=["Terminal"],
)


# important: never return an exec operation's `session_info` to a client. Its `ws_url` and
# `control_url` carry the operation's `?secret=` tokens and the cluster endpoint, so a holder
# could talk to LXD directly: outside this proxy, session tracking, the kill paths and
# `watch_grant_while_open`, which revokes a break-glass session mid-flight. The WebSocket below
# mints its own session and never discloses the secret.


@router.websocket("/ws/{instance_name}")
async def terminal_websocket(
    websocket: WebSocket,
    instance_name: str,
    host: str = Query(...),
    project: str = Query("default"),
    user: Optional[AuthenticatedUser] = Depends(ws_user("terminal")),
):
    """Proxy a browser terminal to an LXD exec session in the instance.

    Opened with a ticket from POST /ws-tickets; re-authorized every minute while open and
    closed with 4403 once access ends. A refusal accepts and then closes with a code the page can
    read (core/ws_close.py); a close before accept reaches the browser as a bare 1006.
    """
    from app.core.config import get_settings as _gs
    _settings = _gs()
    # important: gate on auth_enabled alone. Also requiring `_settings.authentik_issuer` would
    # switch the guard off on the misconfiguration that leaves `user` unset; ws_user refuses
    # that server itself.
    if _settings.auth_enabled and user is None:
        await ws_close.refuse(websocket, *refusal_of(websocket))
        return

    await set_org_scope_for_ws(user, interactive=True)

    # important: this is the socket's organisation boundary; the break-glass gate below does not
    # replace it. Refuse rather than raise: HTTPException means nothing once the socket is being
    # negotiated.
    from fastapi import HTTPException as _HTTPExc
    try:
        # The project bound, the tenant's view grant and, behind the rollout flag, its console
        # grant: the one instance gate the re-authorization below repeats every minute.
        project = verify_instance_access(host, project, instance_name, "console")
    except _HTTPExc as exc:
        await ws_close.refuse(websocket, ws_close.FORBIDDEN, "forbidden")
        logger.warning(
            "Terminal WS blocked (%s): %s@%s user=%s",
            exc.detail, instance_name, host, getattr(user, "email", "?"),
        )
        return

    # Break-glass gate (defense in depth). An admin reaching a tenant cluster they do not
    # own must hold an active grant; otherwise reject. Dev/no-user skips inside enforce.
    _bg_grant_id: Optional[int] = None
    _target_org_id: Optional[int] = None
    _target_org_slug: Optional[str] = None
    if user is not None:
        from app.services.break_glass_service import enforce_sensitive_access
        from app.core.org_scope import resolve_cluster_org
        from app.core.database import async_session_maker
        from fastapi import HTTPException as _HTTPException
        async with async_session_maker() as _bg_db:
            try:
                _grant = await enforce_sensitive_access(user, host, _bg_db)
            except _HTTPException:
                await ws_close.refuse(websocket, ws_close.BREAK_GLASS, "break_glass_required")
                logger.warning(f"Terminal WS blocked (break_glass_required): {instance_name}@{host} user={user.email}")
                return
            _bg_grant_id = _grant.id if _grant else None
            _target_org_id, _target_org_slug = await resolve_cluster_org(host, _bg_db)

    try:
        await assert_instance_not_migration_fenced(
            cluster_id=host,
            project=project,
            instance_name=instance_name,
        )
    except MigrationFenceConflict:
        await ws_close.refuse(websocket, ws_close.FENCED, "migration_fenced")
        return

    await websocket.accept()
    user_id = user.user_id if user else "anonymous"
    logger.info(f"Terminal WebSocket connected: {instance_name}@{host} user={user_id}")

    # important: the only emitter of terminal.session.create. Without it, opening a root shell in
    # a guest would be recorded in session tracking alone and never reach the activity index.
    activity_logger.log_activity(
        action="terminal.session.create",
        target_type="instance",
        target_id=instance_name,
        target_host=host,
        target_project=project,
        result="success",
        details={"transport": "websocket", "break_glass_grant_id": _bg_grant_id},
        target_organization_id=_target_org_id,
        target_organization_slug=_target_org_slug,
    )

    # Extract user details for session tracking
    user_email = user.email if user else ""
    # Resolve via RBAC to the canonical role name (admin/partner/tenant).
    if user:
        from app.core.rbac import resolve_roles, get_highest_role
        _roles = resolve_roles(user)
        user_role = get_highest_role(_roles).value
    else:
        user_role = "unknown"

    proxy = TerminalProxy(
        browser_ws=websocket,
        instance_name=instance_name,
        host=host,
        project=project,
        user_id=user_id,
        user_email=user_email,
        user_role=user_role,
        target_organization_id=_target_org_id,
        target_org_slug=_target_org_slug,
        break_glass_grant_id=_bg_grant_id,
    )
    
    # A break-glass session must not outlive the grant that authorized it: the gate
    # above runs once, at connect. The watchdog closes the socket when the grant
    # expires or is revoked mid-session.
    _bg_watch = None
    if _bg_grant_id:
        from app.services.break_glass_service import watch_grant_while_open
        _bg_watch = asyncio.create_task(watch_grant_while_open(_bg_grant_id, websocket))
    reauthorize = asyncio.create_task(reauthorize_while_open(
        websocket, user, instance_session_check(host, project, instance_name), label=f"terminal {instance_name}@{host}",
    ))
    try:
        await proxy.run()
    finally:
        reauthorize.cancel()
        if _bg_watch:
            _bg_watch.cancel()


@router.get(
    "/health",
    summary="Terminal service health check",
)
async def terminal_health():
    """Report that the terminal router is up. Contacts no cluster."""
    return {"status": "ok", "service": "terminal"}
