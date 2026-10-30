"""
Terminal WebSocket Router.
Provides WebSocket endpoints for interactive terminal access to LXD instances.
"""

import asyncio
import logging
from typing import Optional
from fastapi import APIRouter, WebSocket, Query, Depends

from app.services.terminal_proxy import TerminalProxy
from app.services.activity_logger import get_activity_logger
from app.core.dependencies import AuthenticatedUser
from app.core.instance_access import verify_instance_access
from app.core.ws_auth import ws_user
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


# note: `GET /terminal/session` used to live here. It created a real LXD exec operation and
# returned `session_info` verbatim, which carries `ws_url` and `control_url` complete with the
# operation's `?secret=` file-descriptor tokens and the cluster endpoint. Anyone holding that
# response could talk to LXD directly: outside this proxy, outside session tracking, outside the
# kill paths, and outside `watch_grant_while_open`, which is what revokes a break-glass session
# mid-flight. Measured before removing it: zero calls in seven days and no caller anywhere in
# `frontend/src`. The app uses the WebSocket below, which mints its own session internally and
# never discloses the secret. Removed rather than redacted, because without the secret the
# endpoint answers nothing anyone wanted.


@router.websocket("/ws/{instance_name}")
async def terminal_websocket(
    websocket: WebSocket,
    instance_name: str,
    host: str = Query(...),
    project: str = Query("default"),
    user: Optional[AuthenticatedUser] = Depends(ws_user("terminal")),
):
    """
    WebSocket endpoint for terminal connection.
    Acts as a proxy between browser and LXD instance exec.
    Opened with a ticket from POST /ws-tickets; re-authorized every minute while open and
    closed with 4403 once access ends.
    """
    # Validate auth when enabled
    from app.core.config import get_settings as _gs
    _settings = _gs()
    # important: gated on auth_enabled ALONE. This used to also require
    # `_settings.authentik_issuer`, which is the variable whose absence produces the
    # unauthenticated `user`, so the guard switched itself off in exactly the situation it
    # existed to catch. ws_user now refuses a misconfigured server itself.
    if _settings.auth_enabled and user is None:
        await websocket.close(code=1008, reason="Authentication required")
        return

    # set org scope for WebSocket (context variable)
    await set_org_scope_for_ws(user, interactive=True)

    # important: the console WebSocket added this and the terminal one never had it, so the
    # organisation boundary here rested entirely on the break-glass gate below and on a flag whose
    # default was off. verify_cluster_access appeared seventeen times in console.py and not
    # once here, which is why this file was blanket-exempt from the cluster-scope ratchet; that
    # exemption is gone now, so the ratchet enforces this call. Closing rather than raising,
    # since HTTPException is meaningless once the socket is being negotiated.
    from fastapi import HTTPException as _HTTPExc
    try:
        # The project bound, the tenant's view grant and, behind the rollout flag, its console
        # grant: the one instance gate the re-authorization below repeats every minute.
        project = verify_instance_access(host, project, instance_name, "console")
    except _HTTPExc as exc:
        await websocket.close(code=1008, reason="forbidden")
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
                await websocket.close(code=1008, reason="break_glass_required")
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
        await websocket.close(code=1008, reason="Instance fenced by Migration Center")
        return

    await websocket.accept()
    user_id = user.user_id if user else "anonymous"
    logger.info(f"Terminal WebSocket connected: {instance_name}@{host} user={user_id}")

    # important: this is the only emitter of terminal.session.create now, and until this line
    # there was none on the socket at all. The removed REST endpoint carried it, so a client
    # that connected straight to the WebSocket, which is what the app itself does, opened a root
    # shell in a guest and left nothing in the activity index. Session tracking recorded it; the
    # index a human reviews did not.
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
    # resolve via RBAC to get canonical role name (admin/partner/tenant)
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
    """Check if terminal service is available."""
    return {"status": "ok", "service": "terminal"}
