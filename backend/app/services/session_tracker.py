"""
Console/Terminal Session Tracker - live session intelligence.

Tracks WebSocket console/terminal sessions: duration, per-session bandwidth,
download/upload rate, connection status, who is connected, and protocol type.

Architecture:
- The owning worker holds the authoritative SessionInfo. Byte counters are mutated at
  frame rate by the proxy, so they must stay in-process and must never touch Redis
  per frame.
- A publisher loop shares this worker's live sessions through session_registry, so a
  poll answered by any worker sees the whole fleet. See session_registry.py for why
  that is necessary at all (short version: the WebSocket pins to one of four uvicorn
  workers, the REST poll does not).
- Disconnected sessions are cleaned up after a TTL.

important: the publisher is deliberately per-worker and NOT leased, unlike the metrics
collector. The collector reads a fact that is identical from any worker, so doing it
four times is duplication. This reports a fact only this worker holds, because the
socket is pinned here, so leasing it would silence three quarters of the truth and
reproduce the exact bug the registry exists to fix. The inconsistency between the two
loops is the correct state.
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

# re-exported so existing importers (routers/console.py, routers/terminal.py) keep
# working against the tracker module rather than reaching for the model module.
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
# happens. Keep a strong reference until it finishes. Same defect this module's
# audit writes had.
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

    Needed because register()'s stale-duplicate close only scans the local dict. A
    reconnect that lands on a different worker leaves the old worker's entry alive,
    and nothing noticed while reads only ever saw one worker. Now that reads see all
    of them, the operator would get two rows for one terminal. Not theoretical: both
    LXD sockets run with ping_interval=None, so closing a laptop lid leaves the old
    proxy up until the browser's close finally arrives.

    The key is exactly the tuple register() matches on, so the two cannot disagree.
    Note protocol is deliberately NOT in it, mirroring register(): a terminal and a
    graphics console on the same instance already suppress each other, and quietly
    "improving" that here would make the two paths diverge.
    """
    newest: Dict[Tuple[str, str, str], SessionInfo] = {}
    passthrough: List[SessionInfo] = []
    for s in sessions:
        # an empty email is not an identity: without this every anonymous session on
        # one instance would collapse into a single row.
        if not s.user_email:
            passthrough.append(s)
            continue
        key = (s.instance_name, s.host, s.user_email)
        current = newest.get(key)
        if current is None or s.started_at > current.started_at:
            newest[key] = s
    return passthrough + list(newest.values())


class SessionTracker:
    """
    Global in-memory session store.

    Thread-safe for asyncio (single-threaded event loop).
    Stale disconnected sessions are cleaned up periodically.
    """

    # how long to keep disconnected sessions before cleanup (seconds)
    CLEANUP_TTL = 300  # 5 minutes

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
        # close any existing session for the same instance+user+host
        # (prevents stale sessions from piling up on reconnect)
        for sid, s in list(self._sessions.items()):
            if (s.instance_name == instance_name
                    and s.host == host
                    and s.user_email == user_email
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
        # Fire audit log (non-blocking)
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
        # stays synchronous: it is called from the WebSocket hot path and already uses
        # the house fire-and-forget pattern for the audit write. Waking the publisher
        # gets the session shared immediately without making this async.
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
            # Fire audit log (non-blocking)
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
            # Drop it from the shared view now. Without this the row would linger on
            # other workers until it went stale, so closing a terminal would leave it
            # listed for seconds. The staleness window is the backstop, not the path.
            _spawn(get_session_registry().remove(session_id))
            self.notify_change()

    def live_sessions(self) -> List[SessionInfo]:
        """
        This worker's sessions that have not disconnected.

        Single source of the "is it live" rule. The publisher MUST go through this
        rather than walking _sessions directly: unregister() only MARKS a session
        disconnected and leaves the object in place for CLEANUP_TTL, so a publisher
        iterating the raw dict would re-write the field it had just deleted, one
        heartbeat later, for five minutes. A session that ended would keep coming back.
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

        important: user_email=None means "no user filter" here, which is why the HTTP
        layer must establish who the caller is before it calls anything. A token with
        no email claim used to reach this with None and get back every session on the
        worker, across tenants. Callers that represent a user go through list_active().
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

        Local-first is not just an optimisation: our own SessionInfo has frame-current
        byte counters, while a snapshot is at best one heartbeat old. It also means the
        owning worker still answers correctly when Redis is away, which is what makes
        the degraded path fall out instead of needing a special case.
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

        Lifecycle edges need this. A console registers as CONNECTING and only reaches
        CONNECTED after two LXD connects with 10s and 15s timeouts, so waiting for a
        timer would show the session late, and an ended session would linger.

        note: Event.set() is not thread-safe, which is fine here because every caller
        (register, mark_connected, unregister) runs on the event loop thread.
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

        note: the try sits INSIDE the while on purpose. Wrapping the loop instead means
        the first transient error ends the beat permanently and the whole fleet quietly
        reverts to per-worker views, which is the bug this exists to fix, now silent.
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
                await asyncio.sleep(60)  # check every minute
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


# Singleton instance
_tracker: Optional[SessionTracker] = None


def get_session_tracker() -> SessionTracker:
    """Get the global session tracker singleton."""
    global _tracker
    if _tracker is None:
        _tracker = SessionTracker()
    return _tracker
