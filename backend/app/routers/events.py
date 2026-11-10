"""
WebSocket router streaming LXD operation events to the browser.

Backs live instance-creation progress on the dashboard: one browser connection
subscribes to one cluster's shared event stream (``app/services/lxd/events_hub.py``).
It authenticates like the terminal router, with a ticket from POST /ws-tickets
(``app/core/ws_auth.py``).
"""
import asyncio
import logging
import time
from functools import partial
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, Depends, HTTPException

from app.core.dependencies import AuthenticatedUser
from app.core.ws_auth import ws_user
from app.core.org_scope import (
    get_current_allowed_clusters,
    project_is_visible,
    set_org_scope_for_ws,
    verify_cluster_access,
    check_tenant_permission,
)
from app.core.config import get_settings
from app.core.tenant_visibility import tenant_acl_active
from app.services.lxd.events_hub import get_events_hub
from app.services.ws_session_guard import close_when_session_ends

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["Events"])

# How long a subscriber's resolved scope is trusted before it is read again.
#
# important: the scope is resolved at connect and the socket lives as long as the tab. Without a
# re-read, a Partner keeps an unrestricted scope after a second organization is bound to the
# cluster, and a tenant keeps a revoked policy, until the socket drops.
SCOPE_REFRESH_S = 60.0


def frame_is_visible(host: str, payload: dict) -> bool:
    """Whether one parsed event frame may reach the subscriber whose scope is in context.

    important: the hub subscribes with all-projects, so a cluster's stream carries every project's
    frames (instance names, image aliases, progress, error text). Gates, in the REST path's order:

    1. Current cluster membership, so a long-lived socket stops once a binding or grant drops
       this host.
    2. The Partner project bound. `check_tenant_permission` is open without a tenant ACL, so a
       Partner on a shared cluster would otherwise get the other organization's frames. A frame
       with no project reads as `default`, as in the lists.
    3. The tenant ACL per instance, as verify_tenant_permission on the REST path.
    4. A tenant gets nothing the stream cannot attribute to an instance (ex; a neighbour's volume
       create or image download). Admin and Partner still receive those cluster-wide frames.
    """
    # Checked first: for a Partner the project and tenant predicates below default to open (no
    # tenant ACL, no shared-project bound), so they do not imply cluster access.
    allowed_clusters = get_current_allowed_clusters()
    if allowed_clusters is not None and host not in allowed_clusters:
        return False

    project = payload.get("project") or "default"
    if not project_is_visible(host, project):
        return False
    instance = payload.get("instance")
    if instance:
        return bool(check_tenant_permission(host, project, instance, "view"))
    return not tenant_acl_active()


@router.websocket("/ws")
async def events_websocket(
    websocket: WebSocket,
    host: str = Query(..., description="Cluster to stream operation events from"),
    user: Optional[AuthenticatedUser] = Depends(ws_user("events")),
):
    """Stream a cluster's LXD operation events (incl. image-pull progress) to the
    browser. Opened with a ticket from POST /ws-tickets; closes with 4401 when the session ends."""
    settings = get_settings()
    # important: gated on auth_enabled alone. A missing `authentik_issuer` is what produces an
    # unauthenticated `user`, so also requiring it would switch the guard off in the case it
    # exists for. ws_user refuses a misconfigured server itself.
    if settings.auth_enabled and user is None:
        await websocket.close(code=1008, reason="Authentication required")
        return

    # Set org scope (context var) then enforce cluster access for this host.
    await set_org_scope_for_ws(user)
    try:
        verify_cluster_access(host)
    except Exception:
        await websocket.close(code=1008, reason="Forbidden")
        return

    await websocket.accept()
    hub = get_events_hub()
    # The hub judges every frame under a copy of this task's context, taken now, so the scope
    # resolved above decides whether a frame is queued for this subscriber at all.
    queue = await hub.subscribe(host, accept=partial(frame_is_visible, host))
    user_id = user.user_id if user else "anonymous"
    logger.info(f"[events] browser connected host={host} user={user_id}")

    async def _sender():
        # frame_is_visible reads the scope from this task's copy of the context; a `.set()` here
        # updates that copy and `rescope` hands the hub a fresh one for its pre-enqueue check.
        # The wait is bounded by the refresh deadline: a subscriber whose frames are all filtered
        # before enqueue is never woken by one, and a widened scope has to land anyway.
        scope_read_at = time.monotonic()
        while True:
            due_in = SCOPE_REFRESH_S - (time.monotonic() - scope_read_at)
            try:
                payload: Optional[dict] = await asyncio.wait_for(
                    queue.get(), timeout=max(due_in, 0.0)
                )
            except asyncio.TimeoutError:
                payload = None
            if time.monotonic() - scope_read_at >= SCOPE_REFRESH_S:
                await set_org_scope_for_ws(user)
                try:
                    # important: refreshing the context does not authorize. A revoked Partner
                    # binding or Tenant grant removes `host` from the new allowed set, so close
                    # the socket before installing that context in the hub or sending a frame
                    # queued under the stale scope.
                    verify_cluster_access(host)
                except HTTPException:
                    await websocket.close(code=1008, reason="Forbidden")
                    return
                hub.rescope(host, queue)
                scope_read_at = time.monotonic()
            if payload is None:
                continue
            # Judged again under the refreshed scope, so a frame queued just before a narrowing
            # does not go out. The partner-bound ratchet test checks for this call.
            if not frame_is_visible(host, payload):
                continue
            await websocket.send_json(payload)

    async def _receiver():
        # No inbound messages are expected; this only detects the browser closing.
        while True:
            await websocket.receive_text()

    sender = asyncio.create_task(_sender())
    receiver = asyncio.create_task(_receiver())
    session = asyncio.create_task(close_when_session_ends(websocket, user))
    try:
        _, pending = await asyncio.wait(
            {sender, receiver, session}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"[events] ws ended host={host}: {e}")
    finally:
        sender.cancel()
        receiver.cancel()
        session.cancel()
        await hub.unsubscribe(host, queue)
        logger.info(f"[events] browser disconnected host={host}")
