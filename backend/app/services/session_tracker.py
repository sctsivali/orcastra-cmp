"""
Console/terminal session tracker: duration, per-session bandwidth and rates,
connection status, who is connected, and protocol type.

- The owning worker holds the authoritative SessionInfo. The proxy updates byte
  counters at frame rate, so they stay in-process and never touch Redis per frame.
- A publisher loop shares this worker's live sessions through session_registry, so a
  poll answered by any worker sees every session (see session_registry.py).
- Disconnected sessions are cleaned up after CLEANUP_TTL.

important: the publisher runs on every worker and is not leased, unlike the metrics
collector. The collector reads a fact any worker can read; this reports sessions only
this worker holds, because their sockets are pinned here, so a lease would hide them.
"""

import time
import asyncio
import logging
from typing import Dict, List, Optional, Set, Tuple

from app.services.session_audit import fire_and_forget_connect, fire_and_forget_disconnect
from app.services.session_models import (
    BandwidthSample,
    SessionInfo,
    SessionProtocol,
    SessionStatus,
)
from app.services.session_registry import get_session_registry

logger = logging.getLogger(__name__)

# The model types are re-exported for importers of this module (routers/console.py,
# routers/terminal.py).
__all__ = [
    "BandwidthSample",
    "SessionInfo",
    "SessionProtocol",
    "SessionStatus",
    "SessionTracker",
    "get_session_tracker",
    "dedupe_sessions",
]


# important: asyncio only holds a weak reference to a running task, so a bare
# create_task() can be garbage collected mid-flight and the work silently never
# happens. Keep a strong reference until it finishes.
_background: Set[asyncio.Task] = set()


def _spawn(coro) -> None:
    """Run a coroutine in the background without losing it to the collector."""
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        # no loop (sync test or shutdown): nothing to share to, and the staleness
        # window will retire the entry anyway.
        coro.close()
        return
    _background.add(task)
    task.add_done_callback(_background.discard)


def dedupe_sessions(sessions: List[SessionInfo]) -> List[SessionInfo]:
    """
    Collapse the same connection seen more than once, keeping the newest.

    register() closes a stale duplicate only in the local dict, so a reconnect that
    lands on another worker leaves the old worker's entry alive. Both LXD sockets run
    with ping_interval=None, so a closed laptop lid leaves the old proxy up until the
    browser's close arrives. The key is exactly the tuple register() matches on.

    The protocol is part of the key: a console window keeps its Graphics, Text and Serial tabs
    connected side by side, so a shell and a display on one instance are two live sessions, each
    with its own disconnect audit row.
    """
    newest: Dict[Tuple[str, str, str, SessionProtocol], SessionInfo] = {}
    passthrough: List[SessionInfo] = []
    for s in sessions:
        # an empty email is not an identity: without this every anonymous session on
        # one instance would collapse into a single row.
        if not s.user_email:
            passthrough.append(s)
            continue
        key = (s.instance_name, s.host, s.user_email, s.protocol)
        current = newest.get(key)
        if current is None or s.started_at > current.started_at:
            newest[key] = s
    return passthrough + list(newest.values())


class SessionTracker:
    """
    Per-worker in-memory session store, used from the event loop thread only.

    Stale disconnected sessions are cleaned up periodically.
    """

    # Seconds a disconnected session is kept before cleanup.
    CLEANUP_TTL = 300

    def __init__(self) -> None:
        self._sessions: Dict[str, SessionInfo] = {}
        self._cleanup_task: Optional[asyncio.Task] = None
        self._publisher_task: Optional[asyncio.Task] = None
        self._publish_event: Optional[asyncio.Event] = None

    def register(
        self,
        session_id: str,
        instance_name: str,
        host: str,
        project: str,
        protocol: SessionProtocol,
        user_id: str = "anonymous",
        user_email: str = "",
        user_role: str = "unknown",
        target_organization_id: Optional[int] = None,
        target_org_slug: Optional[str] = None,
        break_glass_grant_id: Optional[int] = None,
    ) -> SessionInfo:
        """Register a new session and start tracking."""
        # close any existing session of the same kind for the same instance+user+host
        # (prevents stale sessions from piling up on reconnect)
        for sid, s in list(self._sessions.items()):
            if (s.instance_name == instance_name
                    and s.host == host
                    and s.user_email == user_email
                    and s.protocol == protocol
                    and s.status != SessionStatus.DISCONNECTED):
                logger.info(
                    f"Closing stale session {sid[:12]}... "
                    f"(replaced by new connection for {instance_name}@{host})"
                )
                s.mark_disconnected()

        session = SessionInfo(
            session_id=session_id,
            instance_name=instance_name,
            host=host,
            project=project,
            protocol=protocol,
            user_id=user_id,
            user_email=user_email,
            user_role=user_role,
            target_organization_id=target_organization_id,
            target_org_slug=target_org_slug,
            break_glass_grant_id=break_glass_grant_id,
        )
        self._sessions[session_id] = session
        logger.info(
            f"Session tracked: {session_id[:12]}... "
            f"({protocol.value}) {instance_name}@{host} user={user_id}"
        )
        fire_and_forget_connect(
            session_id=session_id,
            instance_name=instance_name,
            host=host,
            project=project,
            protocol=protocol.value,
            user_id=user_id,
            user_email=user_email,
            user_role=user_role,
            target_organization_id=target_organization_id,
            target_org_slug=target_org_slug,
            break_glass_grant_id=break_glass_grant_id,
        )
        # register() stays synchronous for the WebSocket hot path; waking the publisher
        # shares the session at once.
        self.notify_change()
        return session

    def get(self, session_id: str) -> Optional[SessionInfo]:
        """Get session by ID."""
        return self._sessions.get(session_id)

    def unregister(self, session_id: str) -> None:
        """Mark session as disconnected (will be cleaned up after TTL)."""
        session = self._sessions.get(session_id)
        if session:
            session.mark_disconnected()
            logger.info(
                f"Session ended: {session_id[:12]}... "
                f"duration={session.duration_seconds:.1f}s "
                f"dl={session.bytes_downloaded} ul={session.bytes_uploaded}"
            )
            fire_and_forget_disconnect(
                session_id=session_id,
                instance_name=session.instance_name,
                host=session.host,
                project=session.project,
                protocol=session.protocol.value,
                user_id=session.user_id,
                user_email=session.user_email,
                user_role=session.user_role,
                duration_seconds=int(session.duration_seconds),
                download_bytes=session.bytes_downloaded,
                upload_bytes=session.bytes_uploaded,
                target_organization_id=session.target_organization_id,
                target_org_slug=session.target_org_slug,
                break_glass_grant_id=session.break_glass_grant_id,
            )
            # Drop it from the shared view now; otherwise other workers keep listing a
            # closed terminal until its snapshot goes stale.
            _spawn(get_session_registry().remove(session_id))
            self.notify_change()

    def live_sessions(self) -> List[SessionInfo]:
        """
        This worker's sessions that have not disconnected: the single "is it live" rule.

        important: the publisher must use this instead of _sessions. unregister() only marks
        a session disconnected and keeps it for CLEANUP_TTL, so publishing the raw dict
        would re-write the field just removed and an ended session would come back.
        """
        return [s for s in self._sessions.values() if s.status != SessionStatus.DISCONNECTED]

    def get_active_sessions(
        self,
        instance_name: Optional[str] = None,
        host: Optional[str] = None,
        user_email: Optional[str] = None,
    ) -> List[SessionInfo]:
        """
        This worker's active sessions only.

        important: user_email=None means "no user filter", so a token with no email
        claim would get every session on the worker, across tenants. The HTTP layer
        must establish who the caller is first; callers that represent a user go
        through list_active().
        """
        result = []
        for s in self.live_sessions():
            if instance_name is not None and s.instance_name != instance_name:
                continue
            if host is not None and s.host != host:
                continue
            if user_email is not None and s.user_email != user_email:
                continue
            result.append(s)
        return result

    # ------------------------------------------------------------------
    # Cross-worker reads
    # ------------------------------------------------------------------

    async def _merged_sessions(self) -> Tuple[Dict[str, SessionInfo], bool]:
        """
        Every live session on every worker, local copies winning.

        A local SessionInfo has frame-current byte counters, while a snapshot is at
        best one heartbeat old, and the owning worker still answers correctly when
        Redis is away.
        """
        merged: Dict[str, SessionInfo] = {s.session_id: s for s in self.live_sessions()}

        registry = get_session_registry()
        if not registry.enabled:
            # no Redis configured at all: single-worker or local dev. Not degraded,
            # because there is no shared view to have lost.
            return merged, False

        from app.core.config import get_settings
        stale_seconds = get_settings().session_registry_stale_seconds

        snapshots, degraded = await registry.fetch(stale_seconds)
        for snap in snapshots:
            sid = snap.get("session_id")
            if not sid or sid in merged:
                continue
            if snap.get("status") == SessionStatus.DISCONNECTED.value:
                continue
            merged[sid] = SessionInfo.from_snapshot(snap)
        return merged, degraded

    async def list_active(
        self,
        *,
        instance_name: Optional[str] = None,
        host: Optional[str] = None,
        user_email: Optional[str] = None,
        allowed_clusters: Optional[Set[str]] = None,
    ) -> Tuple[List[SessionInfo], bool]:
        """
        Fleet-wide active sessions for one caller.

        ``user_email`` scopes to that person's own sessions. ``allowed_clusters``
        scopes to an organisation's clusters; None means unrestricted and is only ever
        passed for an admin, matching how the audit endpoint reads scope=org.

        Returns (sessions, degraded).
        """
        merged, degraded = await self._merged_sessions()

        result: List[SessionInfo] = []
        for s in merged.values():
            if instance_name is not None and s.instance_name != instance_name:
                continue
            if host is not None and s.host != host:
                continue
            if user_email is not None and s.user_email != user_email:
                continue
            if allowed_clusters is not None and s.host not in allowed_clusters:
                continue
            result.append(s)

        return dedupe_sessions(result), degraded

    async def resolve(self, session_id: str) -> Tuple[Optional[SessionInfo], bool]:
        """One session by id, from this worker or any other. Returns (session, degraded)."""
        local = self._sessions.get(session_id)
        if local is not None:
            return local, False

        registry = get_session_registry()
        if not registry.enabled:
            return None, False

        from app.core.config import get_settings
        snapshots, degraded = await registry.fetch(get_settings().session_registry_stale_seconds)
        for snap in snapshots:
            if snap.get("session_id") == session_id:
                return SessionInfo.from_snapshot(snap), degraded
        return None, degraded

    # ------------------------------------------------------------------
    # Publishing this worker's sessions
    # ------------------------------------------------------------------

    def notify_change(self) -> None:
        """
        Ask the publisher to write now rather than at the next beat.

        A console registers as CONNECTING and only reaches CONNECTED after two LXD
        connects with 10s and 15s timeouts, so waiting for a timer would show the
        session late, and an ended session would linger. Event.set() is not
        thread-safe; every caller (register, mark_connected, unregister) runs on the
        event loop thread.
        """
        if self._publish_event is not None:
            self._publish_event.set()

    def mark_connected(self, session_id: str) -> None:
        """Promote a session to CONNECTED and publish immediately."""
        session = self._sessions.get(session_id)
        if session:
            session.mark_connected()
            self.notify_change()

    async def start_publisher(self) -> None:
        """Start the per-worker publisher. Called from lifespan startup."""
        if self._publisher_task and not self._publisher_task.done():
            return
        self._publish_event = asyncio.Event()
        self._publisher_task = asyncio.create_task(self._publisher_loop())

    async def _publisher_loop(self) -> None:
        """
        Share this worker's live sessions, on a beat or whenever one changes.

        important: the try stays inside the while. Wrapping the loop instead, the first
        transient error would end the beat for good and every worker would silently
        fall back to its own view.
        """
        from app.core.config import get_settings
        settings = get_settings()
        heartbeat = max(1, settings.session_registry_heartbeat_seconds)
        ttl = max(heartbeat * 3, settings.session_registry_stale_seconds)
        registry = get_session_registry()

        while True:
            try:
                try:
                    await asyncio.wait_for(self._publish_event.wait(), timeout=heartbeat)
                except asyncio.TimeoutError:
                    pass
                self._publish_event.clear()

                live = self.live_sessions()
                if not live:
                    # an idle worker costs nothing: no round trip at all
                    continue
                now = time.time()
                await registry.publish(
                    [s.to_snapshot(registry.worker_id, now) for s in live],
                    ttl_seconds=ttl,
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Session publish error: {e}")

    def stop_publisher(self) -> None:
        """Stop the publisher background task."""
        if self._publisher_task:
            self._publisher_task.cancel()
            self._publisher_task = None

    async def start_cleanup_loop(self) -> None:
        """Start background loop to clean up stale disconnected sessions."""
        if self._cleanup_task and not self._cleanup_task.done():
            return
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def _cleanup_loop(self) -> None:
        """Periodically remove stale disconnected sessions."""
        while True:
            try:
                await asyncio.sleep(60)
                now = time.time()
                stale_ids = [
                    sid for sid, s in self._sessions.items()
                    if s.status == SessionStatus.DISCONNECTED
                    and s.ended_at is not None
                    and (now - s.ended_at) > self.CLEANUP_TTL
                ]
                for sid in stale_ids:
                    del self._sessions[sid]
                if stale_ids:
                    logger.debug(f"Cleaned up {len(stale_ids)} stale sessions")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Session cleanup error: {e}")

    def stop_cleanup(self) -> None:
        """Stop the cleanup background task."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            self._cleanup_task = None


_tracker: Optional[SessionTracker] = None


def get_session_tracker() -> SessionTracker:
    """Get the global session tracker singleton."""
    global _tracker
    if _tracker is None:
        _tracker = SessionTracker()
    return _tracker
