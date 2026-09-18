"""
LXD operation-events hub (shared per-node fan-out).

Maintains ONE upstream WebSocket to each node's
``/1.0/events?type=operation,lifecycle&all-projects=true`` stream and fans the parsed
events out to many browser subscribers. This is the
shared-per-node model: the first subscriber for a node starts the upstream
listener; when the last subscriber leaves, the listener is torn down. The
upstream connection auto-reconnects (with backoff) as long as subscribers remain.

It powers realtime instance-creation progress on the dashboard. LXD reports
image-pull progress via the operation's ``metadata.download_progress`` field, so
the frontend can show "Pulling image… 48%" without polling. See
``app/routers/events.py`` for the browser-facing WebSocket endpoint.

Reuses the terminal service's certificate/SSL plumbing (same Vault-issued client
cert) to connect to LXD over wss.
"""
import asyncio
import contextvars
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

import websockets

from app.services.terminal_service import get_terminal_service

logger = logging.getLogger(__name__)

# Max events buffered per slow subscriber before we drop the oldest. Progress
# updates are cumulative, so dropping stale frames is harmless.
_QUEUE_MAXSIZE = 100


@dataclass(eq=False)
class _Subscriber:
    """One browser socket's place in a node's fan-out.

    important: `ctx` is the scope the frame is judged under, captured in the subscriber's own
    task when it subscribed (after its org scope was resolved) and replaced by `rescope` after
    each refresh. `_fanout` runs in the node's reader task, which was created under the FIRST
    subscriber's context, so reading the scope contextvars there directly would judge every
    subscriber's frames by one subscriber's rights.
    """

    queue: asyncio.Queue
    accept: Optional[Callable[[dict], bool]]
    ctx: contextvars.Context
    accept_failed: bool = False


class _NodeStream:
    """Per-node upstream connection + its subscribers, keyed by their queue."""

    def __init__(self, host: str):
        self.host = host
        self.subscribers: Dict[asyncio.Queue, _Subscriber] = {}
        self.task: Optional[asyncio.Task] = None


class LXDEventsHub:
    def __init__(self):
        self._nodes: Dict[str, _NodeStream] = {}
        self._lock = asyncio.Lock()

    async def subscribe(
        self, host: str, accept: Optional[Callable[[dict], bool]] = None
    ) -> asyncio.Queue:
        """Register a subscriber for ``host`` and ensure its upstream listener
        is running. Returns the queue the caller should drain.

        `accept` is judged for every frame BEFORE it is queued, under a copy of the calling
        task's context taken now, so call this after the subscriber's scope is set. A frame
        the subscriber may not see never costs it a queue slot: on a busy shared host a
        tenant's queue used to fill with neighbours' frames and drop-oldest evicted its own.
        """
        async with self._lock:
            node = self._nodes.get(host)
            if node is None:
                node = _NodeStream(host)
                self._nodes[host] = node
            queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
            node.subscribers[queue] = _Subscriber(
                queue=queue, accept=accept, ctx=contextvars.copy_context()
            )
            if node.task is None or node.task.done():
                node.task = asyncio.create_task(self._run_node(host))
            logger.info(
                f"[events] subscriber added for {host} (total={len(node.subscribers)})"
            )
            return queue

    async def unsubscribe(self, host: str, queue: asyncio.Queue) -> None:
        """Drop a subscriber; tear down the upstream listener if it was the last."""
        async with self._lock:
            node = self._nodes.get(host)
            if not node:
                return
            node.subscribers.pop(queue, None)
            remaining = len(node.subscribers)
            logger.info(f"[events] subscriber removed for {host} (remaining={remaining})")
            if not node.subscribers:
                if node.task and not node.task.done():
                    node.task.cancel()
                self._nodes.pop(host, None)

    def rescope(self, host: str, queue: asyncio.Queue) -> None:
        """Replace the context a subscriber's frames are judged under.

        Called from the subscriber's own task right after it re-resolved its scope, so the
        copy carries the refreshed contextvars. One attribute write on the loop thread.
        """
        node = self._nodes.get(host)
        sub = node.subscribers.get(queue) if node else None
        if sub is not None:
            sub.ctx = contextvars.copy_context()

    async def shutdown(self) -> None:
        """Cancel every node's upstream listener (called on app shutdown)."""
        async with self._lock:
            nodes = list(self._nodes.values())
            self._nodes.clear()
        for node in nodes:
            if node.task and not node.task.done():
                node.task.cancel()

    def _fanout(self, host: str, payload: dict) -> None:
        node = self._nodes.get(host)
        if not node:
            return
        for sub in list(node.subscribers.values()):
            if sub.accept is not None:
                try:
                    allowed = sub.ctx.run(sub.accept, payload)
                except Exception:
                    # Fail closed, and keep the reader loop alive for everyone else: the
                    # caller of _fanout catches Exception around the whole upstream
                    # connection and would reconnect the node for one bad predicate.
                    if not sub.accept_failed:
                        sub.accept_failed = True
                        logger.exception(
                            f"[events] subscriber predicate raised for {host}; its frames are dropped"
                        )
                    continue
                if not allowed:
                    continue
            q = sub.queue
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # Slow consumer: drop the oldest frame to keep progress fresh.
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except Exception:
                    pass

    def _build_ws_url(self, host: str) -> Optional[str]:
        """Resolve the node's wss events URL from its Vault-stored endpoint."""
        ts = get_terminal_service()
        info = ts.cert_manager.get_cluster_info(host) or {}
        endpoint = info.get("metadata", {}).get("endpoint", "")
        if not endpoint:
            return None
        ws_endpoint = (
            endpoint.replace("https://", "wss://").replace("http://", "ws://").rstrip("/")
        )
        # operation: instance create/start/stop progress (+ image-pull progress).
        # lifecycle: instance created/started/stopped/deleted/... so the dashboard
        # can keep its list current from the stream instead of polling.
        #
        # important: all-projects. Without it LXD emits the default project's events only, so a
        # create into any other project produced no frame at all: the "Creating..." placeholder
        # sat until its ten-minute expiry while the instance had been running for nine of them.
        # Every frame names its project. The hub judges each frame under the subscriber's
        # captured scope (tenant ACL, Partner project bound) before it is queued, and
        # routers/events.py judges it again under the refreshed scope before sending.
        return f"{ws_endpoint}/1.0/events?type=operation,lifecycle&all-projects=true"

    async def _run_node(self, host: str) -> None:
        """Keep an upstream events connection alive while subscribers exist,
        reconnecting with exponential backoff on drop."""
        backoff = 1
        while True:
            node = self._nodes.get(host)
            if not node or not node.subscribers:
                return
            try:
                ws_url = await asyncio.to_thread(self._build_ws_url, host)
                if not ws_url:
                    logger.warning(f"[events] no endpoint for {host}; retrying in 5s")
                    await asyncio.sleep(5)
                    continue
                ssl_ctx = await asyncio.to_thread(
                    get_terminal_service().get_ssl_context, host
                )
                logger.info(f"[events] connecting upstream {host} -> {ws_url}")
                async with websockets.connect(
                    ws_url,
                    ssl=ssl_ctx,
                    ping_interval=30,
                    ping_timeout=60,
                    max_size=4 * 1024 * 1024,
                ) as ws:
                    backoff = 1
                    async for raw in ws:
                        payload = self._parse_event(host, raw)
                        if payload:
                            self._fanout(host, payload)
                        n = self._nodes.get(host)
                        if not n or not n.subscribers:
                            return
            except asyncio.CancelledError:
                logger.info(f"[events] upstream cancelled for {host}")
                return
            except Exception as e:
                logger.warning(
                    f"[events] upstream error for {host}: {e}; reconnecting in {backoff}s"
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    @staticmethod
    def _parse_event(host: str, raw: Any) -> Optional[dict]:
        """Flatten a raw LXD event into a compact payload for the browser.

        Handles two event types:
          - operation: create/start/stop progress (incl. image-pull progress)
          - lifecycle: instance-created/started/stopped/deleted/... state changes
        Returns None for anything else.
        """
        try:
            evt = json.loads(raw)
        except Exception:
            return None
        etype = evt.get("type")
        md = evt.get("metadata", {}) or {}

        if etype == "lifecycle":
            action = md.get("action") or ""
            # Only instance-* lifecycle events are relevant to the instance list.
            if not action.startswith("instance-"):
                return None
            return {
                "type": "lifecycle",
                "host": host,
                "action": action,
                "instance": md.get("name"),
                "project": md.get("project") or evt.get("project"),
                "source": md.get("source"),
            }

        if etype != "operation":
            return None
        resources = md.get("resources", {}) or {}
        instances = resources.get("instances", []) or []
        # important: a resource URL outside the default project is
        # `/1.0/instances/<name>?project=<p>`. Taking the last path segment with the query
        # still attached produced `<name>?project=<p>`, which matched no placeholder, so a
        # create that LXD refused in another project never flipped its row to "Failed".
        names: list = []
        url_project: Optional[str] = None
        for u in instances:
            path, _, query = str(u).partition("?")
            names.append(path.rstrip("/").rsplit("/", 1)[-1])
            for pair in query.split("&"):
                key, _, value = pair.partition("=")
                if key == "project" and value:
                    url_project = url_project or value
        inner = md.get("metadata") or {}
        progress = _extract_progress(inner)
        return {
            "type": "operation",
            "host": host,
            "operation_id": md.get("id"),
            "instances": names,
            "instance": names[0] if names else None,
            "project": evt.get("project") or md.get("project") or url_project,
            "description": md.get("description"),
            "status": md.get("status"),
            "status_code": md.get("status_code"),
            "progress": progress,
            "err": md.get("err") or "",
        }


def _extract_progress(inner: Any) -> Optional[str]:
    """Pull a human-readable progress string out of an operation's metadata.

    LXD reports progress under different keys across the create lifecycle:
      - image pull:   {"download_progress": "rootfs: 48% (590kB/s)"}
      - unpack:       {"create_instance_from_image_unpack_progress": "Unpacking image: 100% ...",
                       "progress": {"percent": "100", "stage": "...", "speed": "..."}}
    The flat ``*_progress`` strings are already display-ready; the nested
    ``progress`` dict is only used as a last resort (formatted to a string) so the
    client never receives an object.
    """
    if not isinstance(inner, dict):
        return None
    if isinstance(inner.get("download_progress"), str):
        return inner["download_progress"]
    # Any "<stage>_progress" string (unpack, etc.).
    for key, value in inner.items():
        if key.endswith("_progress") and isinstance(value, str):
            return value
    prog = inner.get("progress")
    if isinstance(prog, str):
        return prog
    if isinstance(prog, dict):
        pct = prog.get("percent")
        stage = prog.get("stage")
        if pct is not None:
            return f"{stage or 'progress'}: {pct}%"
    return None


_hub: Optional[LXDEventsHub] = None


def get_events_hub() -> LXDEventsHub:
    global _hub
    if _hub is None:
        _hub = LXDEventsHub()
    return _hub
