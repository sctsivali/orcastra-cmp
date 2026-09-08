"""
WebSocket router streaming LXD operation events to the browser.

Backs realtime instance-creation progress on the dashboard: one browser
connection subscribes to one node's shared event stream (see
``app/services/lxd/events_hub.py``). Mirrors the terminal router's auth pattern:
the bearer token is passed as the ``token`` query parameter.
"""
import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, Depends

from app.core.dependencies import AuthenticatedUser, get_ws_user
from app.core.org_scope import (
    set_org_scope_for_ws,
    verify_cluster_access,
    check_tenant_permission,
)
from app.core.config import get_settings
from app.core.tenant_visibility import tenant_acl_active
from app.services.lxd.events_hub import get_events_hub

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["Events"])


@router.websocket("/ws")
async def events_websocket(
    websocket: WebSocket,
    host: str = Query(..., description="LXD host node to stream operation events from"),
    user: Optional[AuthenticatedUser] = Depends(get_ws_user),
):
    """Stream a node's LXD operation events (incl. image-pull progress) to the
    browser. Auth token is passed as the ``token`` query parameter."""
    settings = get_settings()
    # important: gated on auth_enabled ALONE. This used to also require
    # `_settings.authentik_issuer`, which is the variable whose absence produces the
    # unauthenticated `user`, so the guard switched itself off in exactly the situation it
    # existed to catch. get_ws_user now refuses a misconfigured server itself.
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
    queue = await hub.subscribe(host)
    user_id = user.user_id if user else "anonymous"
    logger.info(f"[events] browser connected host={host} user={user_id}")

    async def _sender():
        while True:
            payload = await queue.get()
            # Per-subscriber tenant ACL filter: the node's event stream carries
            # every instance/project on the host, so drop frames this user has no
            # view permission for (mirrors verify_tenant_permission on the REST
            # path). Admins/Partners (no ACL) pass everything.
            instance = payload.get("instance")
            if instance:
                if not check_tenant_permission(
                    host, payload.get("project") or "default", instance, "view"
                ):
                    continue
            elif tenant_acl_active():
                # important: a frame this stream cannot attribute to an instance was forwarded to
                # everyone, because the filter above only ran when there was a name to check. On a
                # cluster several tenants share, that is a neighbour's storage-volume create, image
                # download or project operation arriving in this tenant's feed. A tenant has no
                # claim to an event that names nothing of theirs, so it is dropped.
                #
                # Admin and Partner carry no ACL and still receive the full stream: they are the
                # audience these cluster-wide frames exist for, and tenant_acl_active is False for
                # them, so this branch never runs.
                continue
            await websocket.send_json(payload)

    async def _receiver():
        # We don't expect inbound messages; this just detects browser close.
        while True:
            await websocket.receive_text()

    sender = asyncio.create_task(_sender())
    receiver = asyncio.create_task(_receiver())
    try:
        _, pending = await asyncio.wait(
            {sender, receiver}, return_when=asyncio.FIRST_COMPLETED
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
        await hub.unsubscribe(host, queue)
        logger.info(f"[events] browser disconnected host={host}")
