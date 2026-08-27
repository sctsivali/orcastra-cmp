"""
Data model for a single console/terminal session.

Split out of session_tracker so the tracker file stays about lifecycle and the
cross-worker share, while this one stays about what a session IS and how it is
serialized. See session_registry.py for why sessions leave the process at all.
"""

import time
import uuid
from collections import deque
from typing import Dict, Optional, Any
from dataclasses import dataclass, field
from enum import Enum


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
class BandwidthSample:
    """Single bandwidth measurement sample."""
    timestamp: float
    bytes_count: int


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
    # compliance/accountability context
    target_organization_id: Optional[int] = None
    target_org_slug: Optional[str] = None
    break_glass_grant_id: Optional[int] = None
    status: SessionStatus = SessionStatus.CONNECTING
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None

    # cumulative byte counters (updated atomically from proxy)
    bytes_downloaded: int = 0  # LXD -> browser (user downloads/receives)
    bytes_uploaded: int = 0    # browser -> LXD (user uploads/sends)

    # rate tracking: sliding-window samples for smooth rate calc (deque for O(1) popleft)
    _dl_samples: deque = field(default_factory=deque)
    _ul_samples: deque = field(default_factory=deque)

    # Rates as published by the worker that owns this session. Only set when the
    # object was rebuilt from a registry snapshot: the samples above are local to the
    # owning process, so a remote reader has the number but not the window it came
    # from. None means "this is our own session, compute it".
    _remote_dl_rate: Optional[float] = None
    _remote_ul_rate: Optional[float] = None

    # rate window in seconds
    RATE_WINDOW: float = 10.0

    def mark_connected(self) -> None:
        """Mark session as actively connected."""
        self.status = SessionStatus.CONNECTED

    def mark_disconnected(self) -> None:
        """Mark session as disconnected."""
        self.status = SessionStatus.DISCONNECTED
        self.ended_at = time.time()

    def add_download_bytes(self, count: int) -> None:
        """Record bytes flowing from LXD to browser."""
        self.bytes_downloaded += count
        now = time.time()
        self._dl_samples.append(BandwidthSample(now, count))
        self._prune_samples(self._dl_samples, now)

    def add_upload_bytes(self, count: int) -> None:
        """Record bytes flowing from browser to LXD."""
        self.bytes_uploaded += count
        now = time.time()
        self._ul_samples.append(BandwidthSample(now, count))
        self._prune_samples(self._ul_samples, now)

    def _prune_samples(self, samples: deque, now: float) -> None:
        """Remove samples older than the rate window."""
        cutoff = now - self.RATE_WINDOW
        while samples and samples[0].timestamp < cutoff:
            samples.popleft()  # O(1) instead of O(n) list.pop(0)

    @property
    def download_rate_bps(self) -> float:
        """Current download rate in bytes/second (sliding window avg)."""
        if self._remote_dl_rate is not None:
            return self._remote_dl_rate
        return self._calc_rate(self._dl_samples)

    @property
    def upload_rate_bps(self) -> float:
        """Current upload rate in bytes/second (sliding window avg)."""
        if self._remote_ul_rate is not None:
            return self._remote_ul_rate
        return self._calc_rate(self._ul_samples)

    def _calc_rate(self, samples: deque) -> float:
        """Calculate rate from sliding window samples."""
        if not samples:
            return 0.0
        now = time.time()
        self._prune_samples(samples, now)
        if not samples:
            return 0.0
        window = now - samples[0].timestamp
        if window <= 0:
            return 0.0
        total = sum(s.bytes_count for s in samples)
        return total / max(window, 0.1)  # avoid division by zero

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
            # scope=org shows a tenant who is on their clusters right now. Without
            # these an admin's break-glass session is indistinguishable from an
            # ordinary one, which is weaker than the audit trail already exposes.
            "target_org_slug": self.target_org_slug,
            "is_break_glass": self.break_glass_grant_id is not None,
        }

    def to_snapshot(self, worker_id: str, now: Optional[float] = None) -> Dict[str, Any]:
        """
        Flat, JSON-safe form for the cross-worker registry.

        Deliberately hand-built rather than dataclasses.asdict: the sample deques are
        not serializable, and RATE_WINDOW is a real field so it would be included too.

        Carries started_at, never duration_seconds. The reader recomputes duration from
        its own clock, so it advances smoothly on every poll instead of standing still
        between heartbeats and then jumping. Rates are the exception and must be
        snapshotted, because the sliding-window samples are local and cannot be
        reconstructed anywhere else.
        """
        return {
            "session_id": self.session_id,
            "instance_name": self.instance_name,
            "host": self.host,
            "project": self.project,
            "protocol": self.protocol.value,
            "user_id": self.user_id,
            "user_email": self.user_email,
            "user_role": self.user_role,
            "target_organization_id": self.target_organization_id,
            "target_org_slug": self.target_org_slug,
            "break_glass_grant_id": self.break_glass_grant_id,
            "status": self.status.value,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "bytes_downloaded": self.bytes_downloaded,
            "bytes_uploaded": self.bytes_uploaded,
            "download_rate_bps": round(self.download_rate_bps, 1),
            "upload_rate_bps": round(self.upload_rate_bps, 1),
            "last_seen": now if now is not None else time.time(),
            "worker_id": worker_id,
        }

    @classmethod
    def from_snapshot(cls, snap: Dict[str, Any]) -> "SessionInfo":
        """
        Rebuild a session published by another worker.

        Rates are injected rather than recomputed: the samples they came from live in
        the publishing process. Everything else round-trips, so a remote session and a
        local one serialize through the same to_dict() and cannot drift into different
        field sets.
        """
        session = cls(
            session_id=snap.get("session_id", ""),
            instance_name=snap.get("instance_name", ""),
            host=snap.get("host", ""),
            project=snap.get("project", "default"),
            protocol=SessionProtocol(snap.get("protocol", SessionProtocol.TERMINAL.value)),
            user_id=snap.get("user_id", "anonymous"),
            user_email=snap.get("user_email", "") or "",
            user_role=snap.get("user_role", "unknown"),
            target_organization_id=snap.get("target_organization_id"),
            target_org_slug=snap.get("target_org_slug"),
            break_glass_grant_id=snap.get("break_glass_grant_id"),
        )
        session.status = SessionStatus(snap.get("status", SessionStatus.CONNECTED.value))
        session.started_at = float(snap.get("started_at") or time.time())
        session.ended_at = snap.get("ended_at")
        session.bytes_downloaded = int(snap.get("bytes_downloaded") or 0)
        session.bytes_uploaded = int(snap.get("bytes_uploaded") or 0)
        session._remote_dl_rate = float(snap.get("download_rate_bps") or 0.0)
        session._remote_ul_rate = float(snap.get("upload_rate_bps") or 0.0)
        return session




def new_session_id(operation_id: Optional[str], prefix: str) -> str:
    """
    Our identifier for a session, derived from LXD's operation id.

    LXD returns the operation uuid in metadata.id and that is what we key on, so ids
    match what the cluster reports. It is the EMPTY case that matters: the id is now a
    field in a registry shared by every worker, so a blank one would collapse unrelated
    sessions onto a single entry and ending any one of them would delete the others.

    important: the previous console fallback keyed on id(self), a CPython address. That
    is process-local and reused after garbage collection, so it can collide both across
    workers and with a disconnected session still sitting in the dict awaiting cleanup.
    It was also unreachable, because operation_id is always present - just sometimes
    empty - so the guard has to be on falsiness, not on absence.
    """
    cleaned = (operation_id or "").strip()
    return cleaned or f"{prefix}-{uuid.uuid4().hex}"
