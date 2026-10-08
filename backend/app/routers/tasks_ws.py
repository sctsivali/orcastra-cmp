"""WebSocket feed behind the task panel: one socket per tab, fleet scope, deltas only.

Mirrors `routers/events.py` line for line where it matters: the bearer token travels as the
`token` query parameter, an unauthenticated socket closes 1008, the scope is resolved once
with `set_org_scope_for_ws` and re-read every `SCOPE_REFRESH_S`, every frame is judged under
the subscriber's captured context BEFORE it is queued and again under the refreshed scope
before it is sent. Rows are presented (`present.task_row` / `log_row`) after the second
judgement, with the field policy of the caller's tier.

Frames (server to client; the client sends nothing):
  {"type": "hello", "server_time": iso, "follower_active": bool}
  {"type": "task", "task": TaskRecord}      {"type": "log", "entry": ClusterLogEntry}
  {"type": "follower", "active": bool}      {"type": "resync", "reason": str}
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from functools import partial
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from app.core.config import get_settings
from app.core.dependencies import AuthenticatedUser
from app.core.ws_auth import ws_user
from app.services.ws_session_guard import close_when_session_ends
from app.core.org_scope import set_org_scope_for_ws
from app.core.task_scope import caller_tier, log_is_visible, task_is_visible
from app.models.task_schemas import ClusterLogEntry, TaskRecord
from app.routers.events import SCOPE_REFRESH_S
from app.services.task_journal import present, reads
from app.services.task_journal.tailer import get_task_feed_tailer

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tasks", tags=["Tasks"])


def feed_frame_is_visible(frame: Dict[str, Any], *, caller_email: Optional[str]) -> bool:
    """Whether one tailer frame may reach the subscriber whose scope is in context.

    Row frames go through the same predicates the REST page applies per row; the follower
    and resync frames carry no data and pass.
    """
    kind = frame.get("type")
    if kind == "task":
        return task_is_visible(frame["row"])
    if kind == "log":
        return log_is_visible(frame["row"], caller_email=caller_email)
    return kind in ("follower", "resync")


def _present(frame: Dict[str, Any], tier: str) -> Dict[str, Any]:
    kind = frame.get("type")
    if kind == "task":
        record = TaskRecord(**present.task_row(frame["row"], tier=tier))
        return {"type": "task", "task": record.model_dump(mode="json")}
    if kind == "log":
        entry = ClusterLogEntry(**present.log_row(frame["row"], tier=tier))
        return {"type": "log", "entry": entry.model_dump(mode="json")}
    return frame


async def _follower_active() -> bool:
    tailer = get_task_feed_tailer()
    if tailer.follower_state is not None:
        return bool(tailer.follower_state)
    from app.core.database import async_session_maker

    async with async_session_maker() as db:
        return await reads.follower_active(db)


@router.websocket("/ws")
async def tasks_websocket(
    websocket: WebSocket,
    user: Optional[AuthenticatedUser] = Depends(ws_user("tasks")),
):
    """Stream task and cluster-log deltas in the caller's scope. Opened with a ticket from
    POST /ws-tickets; closes with 4401 when the session ends."""
    settings = get_settings()
    # important: gated on auth_enabled ALONE; ws_user refuses a misconfigured server itself.
    if settings.auth_enabled and user is None:
        await websocket.close(code=1008, reason="Authentication required")
        return

    await set_org_scope_for_ws(user)
    caller_email = user.email if user else None

    await websocket.accept()
    tailer = get_task_feed_tailer()
    # The tailer judges every frame under a copy of THIS task's context, taken now, so the
    # scope resolved above is what decides whether a frame is queued for this subscriber.
    queue = await tailer.subscribe(accept=partial(feed_frame_is_visible, caller_email=caller_email))
    user_id = user.user_id if user else "anonymous"
    logger.info(f"[tasks] feed connected user={user_id}")

    try:
        await websocket.send_json(
            {
                "type": "hello",
                "server_time": datetime.now(timezone.utc).isoformat(),
                "follower_active": await _follower_active(),
            }
        )
    except Exception as exc:
        logger.debug(f"[tasks] hello not sent: {exc}")
        await tailer.unsubscribe(queue)
        return

    async def _sender():
        # The scope set above lives in this task's copy of the context. A `.set()` inside
        # this task updates that same copy, and `rescope` hands the tailer a fresh copy for
        # the pre-enqueue check. The wait is bounded by the time the scope is due, because a
        # subscriber whose frames are all filtered before enqueue is never woken by one.
        scope_read_at = time.monotonic()
        while True:
            due_in = SCOPE_REFRESH_S - (time.monotonic() - scope_read_at)
            try:
                frame: Optional[Dict[str, Any]] = await asyncio.wait_for(
                    queue.get(), timeout=max(due_in, 0.0)
                )
            except asyncio.TimeoutError:
                frame = None
            if time.monotonic() - scope_read_at >= SCOPE_REFRESH_S:
                await set_org_scope_for_ws(user)
                tailer.rescope(queue)
                scope_read_at = time.monotonic()
            if frame is None:
                continue
            # Judged again under the refreshed scope: a frame queued just before a narrowing
            # must not go out.
            if not feed_frame_is_visible(frame, caller_email=caller_email):
                continue
            await websocket.send_json(_present(frame, caller_tier()))

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
    except Exception as exc:
        logger.debug(f"[tasks] feed ended: {exc}")
    finally:
        sender.cancel()
        receiver.cancel()
        session.cancel()
        await tailer.unsubscribe(queue)
        logger.info(f"[tasks] feed disconnected user={user_id}")
