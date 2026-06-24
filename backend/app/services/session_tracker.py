"""
Console/Terminal Session Tracker - In-Memory Session Intelligence.

Provides real-time tracking of WebSocket console/terminal sessions:
- Session duration (wall-clock time since connect)
- Bandwidth per session (bytes sent/received through the proxy)
- Download/upload rate (rolling average per second)
- Connection status (connected / disconnected)
- User info (who is connected, their role)
- Protocol type (websocket-terminal / spice-graphics)

Architecture:
- Thread-safe in-memory store (asyncio-friendly)
- Sessions auto-cleaned after disconnect + TTL
- Stats exposed via REST endpoint for frontend polling

Considerations:
- No DB persistence needed - data is ephemeral per WebSocket lifetime
- Cleanup ensures no memory leak from abandoned sessions
- Rate calculation uses sliding window for smooth display
"""

import time
import asyncio
import logging
from typing import Dict, Optional, Any, List
from dataclasses import dataclass, field
from enum import Enum
from app.services.session_audit import fire_and_forget_connect, fire_and_forget_disconnect

logger = logging.getLogger(__name__)


class SessionProtocol(str, Enum):
    """Protocol type for the session."""
    TERMINAL = "websocket-terminal"
    GRAPHICS = "spice-graphics"


class SessionStatus(str, Enum):
    """Connection lifecycle status."""
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"


@dataclass
class SessionInfo:
    """Tracks a single console/terminal session."""
    session_id: str
    instance_name: str
    host: str
    project: str
    protocol: SessionProtocol
    user_id: str = "anonymous"
    user_email: str = ""
    user_role: str = "unknown"
    status: SessionStatus = SessionStatus.CONNECTING
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None

    # cumulative byte counters (updated from the proxy hot path)
    bytes_downloaded: int = 0  # LXD -> browser (user downloads/receives)
    bytes_uploaded: int = 0    # browser -> LXD (user uploads/sends)

    # number of 1-second buckets in the sliding rate window
    RATE_WINDOW: int = 10

    # important: per-second ring buffers, NOT a growing deque. Each bucket stores
    # the wall-second it represents (`*_secs`) alongside its byte count (`*_counts`),
    # so the hot path is O(1) with no allocation and no prune loop: hash the second
    # to an index, reset the bucket if it now holds an older second, then add. Rate
    # is summed lazily on read from buckets still inside the window. Fixed memory.
    _dl_counts: List[int] = field(default_factory=list, repr=False)
    _ul_counts: List[int] = field(default_factory=list, repr=False)
    _dl_secs: List[int] = field(default_factory=list, repr=False)
    _ul_secs: List[int] = field(default_factory=list, repr=False)
    _start_mono: float = field(default_factory=time.monotonic, repr=False)

    def __post_init__(self) -> None:
        self._dl_counts = [0] * self.RATE_WINDOW
        self._ul_counts = [0] * self.RATE_WINDOW
        self._dl_secs = [-1] * self.RATE_WINDOW
        self._ul_secs = [-1] * self.RATE_WINDOW

    def mark_connected(self) -> None:
        """Mark session as actively connected."""
        self.status = SessionStatus.CONNECTED

    def mark_disconnected(self) -> None:
        """Mark session as disconnected."""
        self.status = SessionStatus.DISCONNECTED
        self.ended_at = time.time()

    def add_download_bytes(self, count: int) -> None:
        """Record bytes flowing from LXD to browser. O(1), called per frame."""
        self.bytes_downloaded += count
        sec = int(time.monotonic())
        idx = sec % self.RATE_WINDOW
        if self._dl_secs[idx] != sec:  # bucket holds an older second -> recycle it
            self._dl_secs[idx] = sec
            self._dl_counts[idx] = 0
        self._dl_counts[idx] += count

    def add_upload_bytes(self, count: int) -> None:
        """Record bytes flowing from browser to LXD. O(1), called per frame."""
        self.bytes_uploaded += count
        sec = int(time.monotonic())
        idx = sec % self.RATE_WINDOW
        if self._ul_secs[idx] != sec:  # bucket holds an older second -> recycle it
            self._ul_secs[idx] = sec
            self._ul_counts[idx] = 0
        self._ul_counts[idx] += count

    @property
    def download_rate_bps(self) -> float:
        """Current download rate in bytes/second (sliding window avg)."""
        return self._calc_rate(self._dl_counts, self._dl_secs)

    @property
    def upload_rate_bps(self) -> float:
        """Current upload rate in bytes/second (sliding window avg)."""
        return self._calc_rate(self._ul_counts, self._ul_secs)

    def _calc_rate(self, counts: List[int], secs: List[int]) -> float:
        """Bytes/second over the sliding window, summed lazily on read.

        Sums only buckets whose second still falls inside the window; buckets
        left over from earlier seconds are simply ignored (and recycled on the
        next write). The divisor ramps from 1s up to RATE_WINDOW so a young
        session reports a sensible rate, matching the previous behaviour.
        """
        now_sec = int(time.monotonic())
        cutoff = now_sec - self.RATE_WINDOW + 1
        total = 0
        for i in range(self.RATE_WINDOW):
            if secs[i] >= cutoff:
                total += counts[i]
        if total == 0:
            return 0.0
        elapsed = now_sec - int(self._start_mono)
        window = min(self.RATE_WINDOW, max(1, elapsed))
        return total / window

    @property
    def duration_seconds(self) -> float:
        """Session duration in seconds."""
        end = self.ended_at or time.time()
        return end - self.started_at

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to API response format."""
        return {
            "session_id": self.session_id,
            "instance_name": self.instance_name,
            "host": self.host,
            "project": self.project,
            "protocol": self.protocol.value,
            "started_at": self.started_at,
            "duration_seconds": round(self.duration_seconds, 1),
            "user": {
                "id": self.user_id,
                "email": self.user_email,
                "role": self.user_role,
            },
            "bandwidth": {
                "download_bytes": self.bytes_downloaded,
                "upload_bytes": self.bytes_uploaded,
                "download_rate_bps": round(self.download_rate_bps, 1),
                "upload_rate_bps": round(self.upload_rate_bps, 1),
            },
            "connection_status": self.status.value,
        }


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
    ) -> SessionInfo:
        """Register a new session and start tracking."""
        # close any existing session for the same instance+user+host
        # (prevents stale sessions from piling up on reconnect).
        # important: a SPICE graphics session opens 4 channels (main/display/inputs/
        # cursor) near-simultaneously, each registering its own session for the same
        # instance+host+user. Running this sweep for graphics would make each new
        # channel mark its own siblings disconnected (corrupting the active-session
        # count and emitting spurious disconnect audit rows). Graphics cleanup is
        # handled by unregister() on socket close + the TTL sweep instead.
        if protocol != SessionProtocol.GRAPHICS:
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
        )
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
            )

    def get_active_sessions(
        self,
        instance_name: Optional[str] = None,
        host: Optional[str] = None,
        user_email: Optional[str] = None,
    ) -> list:
        """Get all active (connected) sessions, optionally filtered by instance, host, and user email."""
        result = []
        for s in self._sessions.values():
            if s.status != SessionStatus.DISCONNECTED:
                if instance_name is not None and s.instance_name != instance_name:
                    continue
                if host is not None and s.host != host:
                    continue
                if user_email is not None and s.user_email != user_email:
                    continue
                result.append(s)
        return result

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
