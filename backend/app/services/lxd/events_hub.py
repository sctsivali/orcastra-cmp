"""
LXD operation-events hub (shared per-node fan-out).

Maintains ONE upstream WebSocket to each node's ``/1.0/events?type=operation``
stream and fans the parsed events out to many browser subscribers. This is the
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
import json
import logging
from typing import Any, Dict, Optional, Set

import websockets

from app.services.terminal_service import get_terminal_service

logger = logging.getLogger(__name__)

# Max events buffered per slow subscriber before we drop the oldest. Progress
# updates are cumulative, so dropping stale frames is harmless.
_QUEUE_MAXSIZE = 100


class _NodeStream:
    """Per-node upstream connection + its set of subscriber queues."""

    def __init__(self, host: str):
        self.host = host
        self.subscribers: Set[asyncio.Queue] = set()
        self.task: Optional[asyncio.Task] = None


class LXDEventsHub:
    def __init__(self):
        self._nodes: Dict[str, _NodeStream] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, host: str) -> asyncio.Queue:
        """Register a subscriber for ``host`` and ensure its upstream listener
        is running. Returns the queue the caller should drain."""
        async with self._lock:
            node = self._nodes.get(host)
            if node is None:
                node = _NodeStream(host)
                self._nodes[host] = node
            queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
            node.subscribers.add(queue)
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
            node.subscribers.discard(queue)
            remaining = len(node.subscribers)
            logger.info(f"[events] subscriber removed for {host} (remaining={remaining})")
            if not node.subscribers:
                if node.task and not node.task.done():
                    node.task.cancel()
                self._nodes.pop(host, None)

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
        for q in list(node.subscribers):
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
        return f"{ws_endpoint}/1.0/events?type=operation,lifecycle"

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
        names = [u.rstrip("/").rsplit("/", 1)[-1] for u in instances]
        inner = md.get("metadata") or {}
        progress = _extract_progress(inner)
        return {
            "type": "operation",
            "host": host,
            "operation_id": md.get("id"),
            "instances": names,
            "instance": names[0] if names else None,
            "project": evt.get("project") or md.get("project"),
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
