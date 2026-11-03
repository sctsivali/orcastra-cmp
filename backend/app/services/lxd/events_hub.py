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

The task journal follower (``app/services/task_journal/follower.py``) is the second consumer:
it subscribes in firehose mode and receives every lifecycle action, the operation fields the
browser never needed, and a ``hub`` frame when the upstream connects or drops. A browser
subscriber receives a projection restricted to ``BROWSER_FRAME_KEYS`` and only ``instance-*``
lifecycle, exactly what it received before the follower existed.

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

from app.services.lxd import health
from app.services.lxd_ws import connection_pin, pinned_connection
from app.services.terminal_service import get_terminal_service

logger = logging.getLogger(__name__)

# Max events buffered per slow subscriber before we drop the oldest. Progress
# updates are cumulative, so dropping stale frames is harmless.
_QUEUE_MAXSIZE = 100

# How long a node whose breaker is open waits before the hub dials it again. Most registered
# clusters on a development fleet are dead, and the follower subscribes to all of them.
_TRIPPED_SLEEP_S = 60

# important: the exact key set a browser frame carried before the firehose existed. A browser
# subscriber never receives `requestor`, `class`, `location`, `resources`, `context` or any
# key added later; extending this set is a deliberate change to the browser contract.
BROWSER_FRAME_KEYS = frozenset(
    {
        "type",
        "host",
        "action",
        "instance",
        "project",
        "source",
        "operation_id",
        "instances",
        "description",
        "status",
        "status_code",
        "progress",
        "err",
    }
)

_REQUESTOR_KEYS = ("protocol", "username", "address")
# Lifecycle `context` blocks carry the exec command line and environment; those never leave
# the daemon through here.
_CONTEXT_DROPPED_KEYS = frozenset({"command", "environment", "env", "args", "fds"})


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
    # A firehose subscriber (the task journal follower) gets the full payload, every lifecycle
    # action and the hub's own connect/disconnect frames.
    firehose: bool = False


class _NodeStream:
    """Per-node upstream connection + its subscribers, keyed by their queue."""

    def __init__(self, host: str):
        self.host = host
        self.subscribers: Dict[asyncio.Queue, _Subscriber] = {}
        self.task: Optional[asyncio.Task] = None
        # True while the upstream socket is open, so a firehose subscriber that joins a node
        # a browser already holds open is told "connected" at once and seeds; the upstream
        # loop emits that frame only on connect.
        self.connected = False


class LXDEventsHub:
    def __init__(self):
        self._nodes: Dict[str, _NodeStream] = {}
        self._lock = asyncio.Lock()

    async def subscribe(
        self,
        host: str,
        accept: Optional[Callable[[dict], bool]] = None,
        *,
        firehose: bool = False,
        maxsize: int = _QUEUE_MAXSIZE,
    ) -> asyncio.Queue:
        """Register a subscriber for ``host`` and ensure its upstream listener
        is running. Returns the queue the caller should drain.

        `accept` is judged for every frame BEFORE it is queued, under a copy of the calling
        task's context taken now, so call this after the subscriber's scope is set. A frame
        the subscriber may not see never costs it a queue slot: on a busy shared host a
        tenant's queue used to fill with neighbours' frames and drop-oldest evicted its own.

        `firehose` and `maxsize` are keyword-only so every browser call site keeps its shape.
        """
        async with self._lock:
            node = self._nodes.get(host)
            if node is None:
                node = _NodeStream(host)
                self._nodes[host] = node
            queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
            node.subscribers[queue] = _Subscriber(
                queue=queue, accept=accept, ctx=contextvars.copy_context(), firehose=firehose
            )
            if node.task is None or node.task.done():
                node.task = asyncio.create_task(self._run_node(host))
            elif firehose and node.connected:
                queue.put_nowait({"type": "hub", "host": host, "state": "connected", "error": None})
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
        browser_only_lifecycle = payload.get("type") == "lifecycle" and not str(
            payload.get("action") or ""
        ).startswith("instance-")
        browser_payload: Optional[dict] = None
        for sub in list(node.subscribers.values()):
            if sub.firehose:
                frame = payload
            else:
                if browser_only_lifecycle:
                    continue
                if browser_payload is None:
                    browser_payload = {
                        k: v for k, v in payload.items() if k in BROWSER_FRAME_KEYS
                    }
                frame = browser_payload
            if sub.accept is not None:
                try:
                    allowed = sub.ctx.run(sub.accept, frame)
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
            _enqueue(sub.queue, frame)

    def _notify_firehose(self, host: str, frame: dict) -> None:
        """A hub-state frame for the firehose subscribers only; browsers never see it."""
        node = self._nodes.get(host)
        if not node:
            return
        for sub in list(node.subscribers.values()):
            if sub.firehose:
                _enqueue(sub.queue, frame)

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
            # important: consulted before dialing. The follower subscribes to every registered
            # cluster, and on a fleet where most of them are dead the backoff alone would keep
            # re-dialing each one every 30 s for as long as the process lives.
            if health.is_tripped(host):
                # The follower learns a cluster is down only from this frame; a cluster whose
                # breaker was already open when it subscribed would otherwise never be marked
                # disconnected. The follower writes the state once per change, so one frame
                # per sleep costs one idempotent UPDATE.
                self._notify_firehose(
                    host,
                    {
                        "type": "hub",
                        "host": host,
                        "state": "disconnected",
                        "error": health.get_reason(host) or "circuit breaker open",
                    },
                )
                await asyncio.sleep(_TRIPPED_SLEEP_S)
                continue
            try:
                ws_url = await asyncio.to_thread(self._build_ws_url, host)
                if not ws_url:
                    logger.warning(f"[events] no endpoint for {host}; retrying in 5s")
                    await asyncio.sleep(5)
                    continue
                ssl_ctx = await asyncio.to_thread(
                    get_terminal_service().get_ssl_context, host
                )
                pin = await asyncio.to_thread(connection_pin, host)
                logger.info(f"[events] connecting upstream {host} -> {ws_url}")
                # important: held to the cluster's pin like every other LXD socket (lxd_ws), and
                # never through HTTPS_PROXY. A stranger here could feed forged lifecycle events
                # to every subscriber and to the connection state this hub writes.
                async with websockets.connect(
                    ws_url,
                    ssl=ssl_ctx,
                    ping_interval=30,
                    ping_timeout=60,
                    max_size=4 * 1024 * 1024,
                    proxy=None,
                    create_connection=pinned_connection(pin) if pin else None,
                ) as ws:
                    backoff = 1
                    node.connected = True
                    self._notify_firehose(
                        host, {"type": "hub", "host": host, "state": "connected", "error": None}
                    )
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
                node.connected = False
                logger.warning(
                    f"[events] upstream error for {host}: {e}; reconnecting in {backoff}s"
                )
                self._notify_firehose(
                    host,
                    {"type": "hub", "host": host, "state": "disconnected", "error": str(e)},
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    @staticmethod
    def _parse_event(host: str, raw: Any) -> Optional[dict]:
        """Flatten a raw LXD event into the full payload; `_fanout` projects it per subscriber.

        Handles two event types:
          - operation: create/start/stop progress (incl. image-pull progress)
          - lifecycle: every resource's created/started/stopped/deleted/... state change
        Returns None for anything else.

        important: the operation's inner `metadata` is read for a progress string only. It is
        where LXD puts exec file descriptors and the command line, and nothing here ever copies
        it into a frame.
        """
        try:
            evt = json.loads(raw)
        except Exception:
            return None
        etype = evt.get("type")
        md = evt.get("metadata", {}) or {}

        if etype == "lifecycle":
            action = md.get("action") or ""
            if not action:
                return None
            name = md.get("name")
            return {
                "type": "lifecycle",
                "host": host,
                "action": action,
                # `instance` is what the browser's instance list keys on, so it names an
                # instance and nothing else; `name` is the resource whatever its kind.
                "instance": name if action.startswith("instance-") else None,
                "name": name,
                "project": md.get("project") or evt.get("project"),
                "source": md.get("source"),
                "requestor": _requestor(md.get("requestor")),
                "context": _context(md.get("context")),
                "timestamp": evt.get("timestamp"),
            }

        if etype != "operation":
            return None
        resources = md.get("resources", {}) or {}
        # important: a resource URL outside the default project is
        # `/1.0/instances/<name>?project=<p>`. Taking the last path segment with the query
        # still attached produced `<name>?project=<p>`, which matched no placeholder, so a
        # create that LXD refused in another project never flipped its row to "Failed".
        resource_names: Dict[str, list] = {}
        url_project: Optional[str] = None
        for kind, urls in resources.items():
            if not isinstance(urls, list):
                continue
            names: list = []
            for u in urls:
                path, _, query = str(u).partition("?")
                names.append(path.rstrip("/").rsplit("/", 1)[-1])
                for pair in query.split("&"):
                    key, _, value = pair.partition("=")
                    if key == "project" and value:
                        url_project = url_project or value
            resource_names[str(kind)] = names
        instances = resource_names.get("instances", [])
        inner = md.get("metadata") or {}
        progress = _extract_progress(inner)
        return {
            "type": "operation",
            "host": host,
            "operation_id": md.get("id"),
            "instances": instances,
            "instance": instances[0] if instances else None,
            "project": evt.get("project") or md.get("project") or url_project,
            "description": md.get("description"),
            "status": md.get("status"),
            "status_code": md.get("status_code"),
            "progress": progress,
            "err": md.get("err") or "",
            "class": md.get("class"),
            "may_cancel": bool(md.get("may_cancel")),
            "created_at": md.get("created_at"),
            "updated_at": md.get("updated_at"),
            "location": md.get("location"),
            "resources": resource_names,
            "requestor": _requestor(md.get("requestor")),
            "timestamp": evt.get("timestamp"),
        }


def _enqueue(q: asyncio.Queue, frame: dict) -> None:
    try:
        q.put_nowait(frame)
    except asyncio.QueueFull:
        # Slow consumer: drop the oldest frame to keep progress fresh.
        try:
            q.get_nowait()
            q.put_nowait(frame)
        except Exception:
            pass


def _requestor(value: Any) -> Optional[dict]:
    if not isinstance(value, dict):
        return None
    return {k: value.get(k) for k in _REQUESTOR_KEYS if value.get(k) is not None}


def _context(value: Any) -> Optional[dict]:
    if not isinstance(value, dict):
        return None
    return {str(k): v for k, v in value.items() if str(k) not in _CONTEXT_DROPPED_KEYS}


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
