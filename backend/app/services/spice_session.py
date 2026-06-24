"""
SPICE Session Manager - one LXD console operation shared by all channels.

A single SPICE console is four WebSocket channels (main/display/inputs/cursor).
spice-html5 opens one WebSocket per channel, all to the same URL/ticket. Each of
those reaches our backend as a separate connection, but they belong to ONE SPICE
session and must map to ONE LXD console operation - exactly how the native LXD-UI
works (one `console.post`, one control socket, N data sockets all reusing the same
data-fd secret; LXD allows multiple connections to that secret).

Previously each channel created its own LXD console operation (4 operations, 4
control + 4 data upstream sockets per console). That fan-out was the root of the
"Display output is not active / must reconnect" first-connect failures (any of the
four operations being slow or failing dropped a channel) and a chunk of the felt
lag (partial/flaky channels), not the canvas decoder.

This manager owns one operation per SPICE session (keyed by the shared ticket),
ref-counts the channels, opens the single control socket, and tears the operation
down when the last channel leaves.
"""

import asyncio
import logging
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional, Any, List, Tuple

import websockets
from websockets.exceptions import ConnectionClosed

from app.services.console_service import get_console_service
from app.services.session_tracker import get_session_tracker, SessionProtocol

logger = logging.getLogger(__name__)


class ConsoleSessionError(Exception):
    """Raised when the shared LXD console operation cannot be created."""


def _get_client(host: str):
    """Best-effort LXD client for ``host`` (sync; call via asyncio.to_thread)."""
    from app.services.lxd_service import get_lxd_service
    return get_lxd_service().connection_manager.get_client(host)


def _cancel_operation_sync(host: str, op_id: str) -> bool:
    """Cancel one LXD operation. Best-effort; runs in a worker thread."""
    if not op_id:
        return False
    client = _get_client(host)
    if not client:
        return False
    try:
        client.api.operations[op_id].delete()
        return True
    except Exception as e:  # noqa: BLE001 - cancellation is best-effort
        logger.debug("Cancel op=%s on %s failed: %s", op_id, host, e)
        return False


def _list_console_ops_sync(host: str) -> List[Tuple[str, str]]:
    """Running console operations on ``host`` as (op_id, created_at).

    Matches "Showing console" (VGA/text console) by description, NOT
    "Executing command" (exec/terminal), so reaping never touches exec sessions.
    """
    client = _get_client(host)
    if not client:
        return []
    out: List[Tuple[str, str]] = []
    try:
        resp = client.api.operations.get(params={"recursion": "1"}, timeout=(5, 30))
        if resp.status_code == 200:
            for op in resp.json().get("metadata", {}).get("running", []):
                if "console" in (op.get("description") or "").lower():
                    out.append((op.get("id", ""), op.get("created_at", "")))
    except Exception as e:  # noqa: BLE001
        logger.debug("List console ops on %s failed: %s", host, e)
    return out


def _age_seconds(created_at: str, now: datetime) -> Optional[float]:
    """Age in seconds of an LXD ISO-8601 timestamp (handles ns precision + Z)."""
    if not created_at:
        return None
    try:
        s = created_at.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        if "." in s:
            head, frac = s.split(".", 1)
            tz = ""
            for i, ch in enumerate(frac):
                if ch in "+-":
                    tz, frac = frac[i:], frac[:i]
                    break
            s = f"{head}.{frac[:6]}{tz}"  # trim ns -> us so fromisoformat accepts it
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (now - dt).total_seconds()
    except Exception:  # noqa: BLE001
        return None


@dataclass
class SpiceSession:
    """One shared LXD console operation for a single SPICE session.

    Holds the operation metadata, the single control socket (+ its drain task)
    and the tracker entry. Every channel of the console opens its own data
    socket to ``data_url`` but shares this one operation and control socket.
    """
    key: str
    session_info: Optional[Dict[str, Any]] = None
    ssl_context: Optional[ssl.SSLContext] = None
    control_ws: Any = None
    refcount: int = 0
    tracked: Any = None  # SessionInfo from the tracker (one per console)
    _control_task: Optional[asyncio.Task] = field(default=None, repr=False)
    _create_task: Optional[asyncio.Task] = field(default=None, repr=False)

    @property
    def data_url(self) -> str:
        return self.session_info["ws_url"] if self.session_info else ""

    @property
    def operation_id(self) -> str:
        return self.session_info.get("operation_id", "") if self.session_info else ""


class SpiceSessionManager:
    """Owns one LXD console operation per SPICE session, shared by its channels."""

    def __init__(self) -> None:
        self._sessions: Dict[str, SpiceSession] = {}
        self._lock = asyncio.Lock()

    async def acquire(
        self,
        key: str,
        *,
        instance_name: str,
        host: str,
        project: str,
        console_type: str = "vga",
        user_id: str = "anonymous",
        user_email: str = "",
        user_role: str = "unknown",
    ) -> SpiceSession:
        """Get (or create once) the shared operation for ``key`` and add a channel.

        The first channel for a key creates the operation; concurrent siblings
        await the same in-flight creation and reuse it. Callers MUST pair this
        with ``release(key)`` (ConsoleProxy does so in its cleanup).
        """
        async with self._lock:
            sess = self._sessions.get(key)
            if sess is None:
                sess = SpiceSession(key=key)
                sess.refcount = 1
                self._sessions[key] = sess
                sess._create_task = asyncio.create_task(
                    self._create(
                        sess,
                        instance_name=instance_name,
                        host=host,
                        project=project,
                        console_type=console_type,
                        user_id=user_id,
                        user_email=user_email,
                        user_role=user_role,
                    )
                )
            else:
                sess.refcount += 1

        # All channels (creator and siblings) await the single creation; it
        # raises ConsoleSessionError on failure. On failure, drop the entry from
        # the registry immediately (exactly once) so a fresh acquire rebuilds
        # cleanly instead of inheriting the cached failure during the reap window.
        try:
            await sess._create_task
        except Exception:
            async with self._lock:
                removed = self._sessions.pop(key, None) is sess
            if removed:
                await self._teardown(sess)
            raise
        return sess

    async def _create(
        self,
        sess: SpiceSession,
        *,
        instance_name: str,
        host: str,
        project: str,
        console_type: str,
        user_id: str,
        user_email: str,
        user_role: str,
    ) -> None:
        console_service = get_console_service()
        session_info, error = await console_service.create_console_session(
            instance_name=instance_name,
            host=host,
            project=project,
            console_type=console_type,
            width=1024,
            height=768,
        )
        if error or not session_info:
            raise ConsoleSessionError(error or "Failed to create console session")

        sess.session_info = session_info
        # get_ssl_context may fetch from Vault on a cache miss (blocking); offload
        # so it never stalls the single-worker event loop.
        sess.ssl_context = await asyncio.to_thread(console_service.get_ssl_context, host)

        # Single control socket for the whole operation (LXD-UI opens exactly one).
        # It must be connected for the operation to stream, and draining it keeps
        # the operation alive for as long as any channel is attached.
        control_url = session_info.get("control_url", "")
        if control_url:
            sess.control_ws = await asyncio.wait_for(
                websockets.connect(
                    control_url,
                    ssl=sess.ssl_context,
                    close_timeout=10,
                    ping_interval=None,
                    ping_timeout=None,
                ),
                timeout=10.0,
            )
            sess._control_task = asyncio.create_task(self._drain_control(sess))

        # One tracker entry per console (not per channel): every channel adds its
        # bytes to this shared SessionInfo, so the UI sees one session with the
        # correct aggregate bandwidth.
        tracker = get_session_tracker()
        sess.tracked = tracker.register(
            session_id=session_info.get("operation_id", f"spice-{sess.key[:16]}"),
            instance_name=instance_name,
            host=host,
            project=project,
            protocol=SessionProtocol.GRAPHICS,
            user_id=user_id,
            user_email=user_email,
            user_role=user_role,
        )
        sess.tracked.mark_connected()
        logger.info(
            "SPICE session created: op=%s %s@%s (one operation, shared by channels)",
            sess.operation_id, instance_name, host,
        )

    async def _drain_control(self, sess: SpiceSession) -> None:
        """Consume the control socket so LXD keeps the operation alive."""
        try:
            async for _message in sess.control_ws:
                pass
        except (ConnectionClosed, asyncio.CancelledError):
            pass
        except Exception as e:  # noqa: BLE001 - defensive drain
            logger.debug("SPICE control drain ended for op=%s: %s", sess.operation_id, e)

    async def release(self, key: str) -> None:
        """Drop one channel from ``key``; tear the operation down at zero."""
        async with self._lock:
            sess = self._sessions.get(key)
            if sess is None:
                return
            sess.refcount -= 1
            if sess.refcount > 0:
                return
            del self._sessions[key]
        await self._teardown(sess)

    async def _teardown(self, sess: SpiceSession) -> None:
        if sess._control_task:
            sess._control_task.cancel()
            try:
                await sess._control_task
            except Exception:  # noqa: BLE001
                pass
        if sess.control_ws:
            try:
                await sess.control_ws.close()
            except Exception:  # noqa: BLE001
                pass
        if sess.tracked:
            try:
                get_session_tracker().unregister(sess.tracked.session_id)
            except Exception:  # noqa: BLE001
                pass
        # important: explicitly cancel the LXD console operation. LXD does NOT
        # auto-cancel a console operation when its sockets close (unlike exec), so
        # without this the "Showing console" op stays Running on the cluster as an
        # orphan (confirmed: an op survived a backend restart for >1 day).
        host = sess.session_info.get("host") if sess.session_info else None
        if host and sess.operation_id:
            try:
                await asyncio.to_thread(_cancel_operation_sync, host, sess.operation_id)
            except Exception:  # noqa: BLE001
                pass
        logger.info("SPICE session torn down: op=%s", sess.operation_id)

    def live_operation_ids(self) -> set:
        """Operation IDs of all live sessions (single-worker mini: the full set)."""
        return {s.operation_id for s in self._sessions.values() if s.operation_id}

    async def reap_orphans(self, max_age_seconds: int = 600) -> int:
        """Cancel console operations that no live session owns and that are older
        than ``max_age_seconds``.

        Catches orphans left by an abnormal teardown (backend crash/restart) that
        skipped ``_teardown``. The age floor protects an op whose session is still
        registering. NOTE: assumes a single backend worker (orcastra-mini), where
        ``live_operation_ids`` is the complete live set; do not enable under
        multiple workers without a shared session registry.
        """
        live = self.live_operation_ids()
        try:
            from app.services.lxd_service import get_lxd_service
            hosts = get_lxd_service().connection_manager.get_node_names()
        except Exception:  # noqa: BLE001
            hosts = []
        now = datetime.now(timezone.utc)
        reaped = 0
        for host in hosts:
            for op_id, created_at in await asyncio.to_thread(_list_console_ops_sync, host):
                if not op_id or op_id in live:
                    continue
                age = _age_seconds(created_at, now)
                if age is None or age < max_age_seconds:
                    continue
                if await asyncio.to_thread(_cancel_operation_sync, host, op_id):
                    reaped += 1
                    logger.info("Reaped orphan console op=%s on %s (age=%.0fs)", op_id, host, age)
        return reaped

    def active_count(self) -> int:
        """Number of live shared operations (for tests/diagnostics)."""
        return len(self._sessions)


async def run_console_reaper_loop(interval_s: int = 300, max_age_s: int = 600) -> None:
    """Background loop: periodically reap orphaned console operations."""
    mgr = get_spice_session_manager()
    while True:
        try:
            await asyncio.sleep(interval_s)
            reaped = await mgr.reap_orphans(max_age_seconds=max_age_s)
            if reaped:
                logger.info("Console orphan reaper cancelled %d stale operation(s)", reaped)
        except asyncio.CancelledError:
            break
        except Exception as e:  # noqa: BLE001
            logger.warning("Console reaper error: %s", e)


_manager: Optional[SpiceSessionManager] = None


def get_spice_session_manager() -> SpiceSessionManager:
    """Get the global SPICE session manager singleton."""
    global _manager
    if _manager is None:
        _manager = SpiceSessionManager()
    return _manager
