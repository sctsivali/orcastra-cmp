"""
WebSocket router streaming LXD operation events to the browser.

Backs realtime instance-creation progress on the dashboard: one browser
connection subscribes to one node's shared event stream (see
``app/services/lxd/events_hub.py``). Mirrors the terminal router's auth pattern:
the bearer token is passed as the ``token`` query parameter.
"""
import asyncio
import logging
import time
from functools import partial
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, Depends, HTTPException

from app.core.dependencies import AuthenticatedUser, get_ws_user
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

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["Events"])

# How long a subscriber's resolved scope is trusted before it is read again.
#
# important: the scope is resolved once at connect and the socket lives as long as the tab. A
# Partner who connected while a cluster had one owner keeps an unrestricted scope after a second
# organisation is bound to it, and a tenant keeps a policy that was revoked, until the socket
# drops. That window always existed for the tenant ACL; with the stream carrying every project
# it is worth closing, so a busy socket re-reads its scope at most this often.
SCOPE_REFRESH_S = 60.0


def frame_is_visible(host: str, payload: dict) -> bool:
    """Whether one parsed event frame may reach the subscriber whose scope is in context.

    important: the hub subscribes with all-projects, so the node's stream carries every
    project's operation and lifecycle frames (instance names, image aliases, progress, error
    text). Four gates, in the order the REST path applies them:

    1. Current cluster membership. A long-lived socket must stop when its organization binding
       or tenant grant no longer includes this host.
    2. The Partner project bound. `check_tenant_permission` is no bound for a Partner (it
       returns open for anyone without a tenant ACL), so without this a Partner on a cluster
       two organisations share would receive the other organisation's frames the moment the
       subscription widened. A frame with no project reads as the default project, the same
       reading the lists give a record with no project.
    3. The tenant ACL, per instance, mirroring verify_tenant_permission on the REST path.
    4. A tenant gets nothing this stream cannot attribute to an instance: a neighbour's
       storage-volume create or image download names nothing of theirs. Admin and Partner
       carry no ACL and still receive those cluster-wide frames, which are for them.
    """
    # The project and tenant predicates below deliberately do not imply cluster access:
    # a Partner has no tenant ACL, and an absent shared-project bound means unrestricted
    # projects. Re-check the cluster context first so a subscriber whose organization binding
    # or tenant grant was revoked cannot keep accepting frames under those permissive defaults.
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
    # The hub judges every frame under a copy of THIS task's context, taken now, so the scope
    # resolved above is what decides whether a frame is queued for this subscriber at all.
    queue = await hub.subscribe(host, accept=partial(frame_is_visible, host))
    user_id = user.user_id if user else "anonymous"
    logger.info(f"[events] browser connected host={host} user={user_id}")

    async def _sender():
        # The scope set above lives in this task's copy of the context, so frame_is_visible
        # reads the same bound and ACL the handler resolved for this subscriber. A `.set()`
        # inside this task updates that same copy, and `rescope` hands the hub a fresh copy
        # for the pre-enqueue check. The wait is bounded by the time the scope is due, because
        # a subscriber whose frames are all filtered before enqueue is never woken by one, and
        # a widened scope (a new project grant) has to land regardless.
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
                    # important: refreshing the context is not itself authorization. A
                    # revoked Partner binding or Tenant grant removes `host` from the new
                    # allowed set, so fail the socket closed before installing that context
                    # in the hub or sending a frame that was queued under the old scope.
                    verify_cluster_access(host)
                except HTTPException:
                    await websocket.close(code=1008, reason="Forbidden")
                    return
                hub.rescope(host, queue)
                scope_read_at = time.monotonic()
            if payload is None:
                continue
            # Judged again under the refreshed scope: a frame queued just before a narrowing
            # must not go out, and this is the site the partner-bound ratchet owns.
            if not frame_is_visible(host, payload):
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
