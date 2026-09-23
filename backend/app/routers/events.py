"""
WebSocket router streaming LXD operation events to the browser.

Backs realtime instance-creation progress on the dashboard: one browser
connection subscribes to one node's shared event stream (see
``app/services/lxd/events_hub.py``). Mirrors the terminal router's auth pattern —
the bearer token is passed as the ``token`` query parameter.
"""
import asyncio
import logging
import time
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, Depends, HTTPException

from app.core.dependencies import AuthenticatedUser, get_ws_user
from app.core.org_scope import (
    set_org_scope_for_ws,
    verify_cluster_access,
    check_tenant_permission,
)
from app.core.config import get_settings
from app.services.lxd.events_hub import get_events_hub

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["Events"])

# important: the scope is read once at connect, and a socket can stay open for hours. A caller
# whose policy was revoked, or whose organization lost the cluster, kept receiving its frames
# until they closed the tab. The scope is re-read at most this often, on the next frame.
SCOPE_REFRESH_S = 60.0


async def _rescope(user: Optional[AuthenticatedUser], host: str) -> bool:
    """Re-read the caller's scope and say whether `host` is still inside it.

    Any failure to re-read is a refusal. set_org_scope_for_ws writes the cluster set before it
    narrows it by the tenant ACL, so a read that failed halfway could leave a wider scope than
    the caller holds.
    """
    try:
        await set_org_scope_for_ws(user)
    except Exception as exc:  # noqa: BLE001 - fail closed, the client reconnects
        logger.warning(f"[events] could not re-read the scope for host={host}: {exc}")
        return False
    try:
        verify_cluster_access(host)
    except HTTPException:
        return False
    return True


@router.websocket("/ws")
async def events_websocket(
    websocket: WebSocket,
    host: str = Query(..., description="LXD host node to stream operation events from"),
    user: Optional[AuthenticatedUser] = Depends(get_ws_user),
):
    """Stream a node's LXD operation events (incl. image-pull progress) to the
    browser. Auth token is passed as the ``token`` query parameter."""
    settings = get_settings()
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
        read_at = time.monotonic()
        while True:
            payload = await queue.get()
            if time.monotonic() - read_at >= SCOPE_REFRESH_S:
                if not await _rescope(user, host):
                    await websocket.close(code=1008, reason="Forbidden")
                    return
                read_at = time.monotonic()
            # Per-subscriber tenant ACL filter: the node's event stream carries
            # every instance/project on the host, so drop frames this user has no
            # view permission for (mirrors verify_tenant_permission on the REST
            # path). Admins/Partners (no ACL) pass everything.
            instance = payload.get("instance")
            if instance and not check_tenant_permission(
                host, payload.get("project") or "default", instance, "view"
            ):
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
