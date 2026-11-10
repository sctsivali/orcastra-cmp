"""
Shared LXD cluster health circuit breaker.

Tracks recently-failed clusters so every connection path (verify_connection,
node-status checks, warning fan-outs, startup) can skip a known-dead cluster
instead of paying the full connect timeout again on each request. Per-process
and thread-safe; a soft optimization that self-heals after the TTL and clears
immediately on any successful connect.
"""

import threading
import time
from typing import Dict, Iterable, Optional, Tuple

# How long a cluster stays "tripped" after a failure before it is probed again.
DEFAULT_TTL = 60.0

# Why the cluster failed. Callers have to tell an offline cluster from one refusing the
# dashboard's certificate, and matching substrings of the free-text reason mislabels them.
KIND_OFFLINE = "offline"
KIND_UNAUTHORIZED = "unauthorized"

_lock = threading.Lock()
# cluster_id -> (failed_at_epoch, reason, kind)
_failed: Dict[str, Tuple[float, str, str]] = {}
# cluster_id -> epoch of the last failure, kept past the TTL until a success clears it. A worker
# that lost its connection stays disconnected after the trip expires (until the healer retries),
# and breaker_sync needs to know that window too.
_last_failed: Dict[str, float] = {}
# cluster_id -> (reason, kind) of that last failure, kept and cleared with _last_failed. A reader that
# skips a known-dead cluster still has to say whether it was offline or refusing the certificate.
_last_verdict: Dict[str, Tuple[str, str]] = {}
# cluster_id -> epoch of this worker's last successful connect. A failure another worker observed
# before it must never be adopted here (see adopt): the local proof is newer.
_cleared_at: Dict[str, float] = {}

# The reason an adopted verdict carries: fixed text, since the observing worker's own reason is raw
# exception text and is not shared.
_ADOPTED_REASON = {
    KIND_OFFLINE: "no connection to the cluster",
    KIND_UNAUTHORIZED: "certificate not trusted",
}


def record_failure(cluster_id: str, reason: str = "", *, kind: str = KIND_OFFLINE) -> None:
    """Mark a cluster as failed so callers skip it until the TTL expires.

    Only the paths that know the host answered pass KIND_UNAUTHORIZED.
    """
    with _lock:
        now = time.time()
        _failed[cluster_id] = (now, reason or "unreachable", kind)
        _last_failed[cluster_id] = now
        _last_verdict[cluster_id] = (reason or "unreachable", kind)


def clear(cluster_id: str) -> None:
    """Un-trip a cluster after a successful connect so recovery is immediate."""
    with _lock:
        _failed.pop(cluster_id, None)
        _last_failed.pop(cluster_id, None)
        _last_verdict.pop(cluster_id, None)
        _cleared_at[cluster_id] = time.time()


def adopt(cluster_id: str, failed_at: float, kind: str) -> bool:
    """Take a failure another worker observed, so this one does not dial the cluster to learn it.

    True when this worker now holds a failure at least as new as `failed_at` (adopted, or it
    already knew). False when this worker cleared the cluster after that observation: its own
    proof outranks an older fleet verdict. Not record_failure: nothing here contacted the peer.
    """
    if kind not in _ADOPTED_REASON:
        return False
    with _lock:
        if _cleared_at.get(cluster_id, 0.0) >= failed_at:
            return False
        known = _last_failed.get(cluster_id)
        if known is not None and known >= failed_at:
            return True
        reason = _ADOPTED_REASON[kind]
        _failed[cluster_id] = (failed_at, reason, kind)
        _last_failed[cluster_id] = failed_at
        _last_verdict[cluster_id] = (reason, kind)
        return True


def is_tripped(cluster_id: str, ttl: float = DEFAULT_TTL) -> bool:
    """True if the cluster failed within the last ``ttl`` seconds."""
    with _lock:
        entry = _failed.get(cluster_id)
        if not entry:
            return False
        if time.time() - entry[0] < ttl:
            return True
        # Expired: evict so the next probe runs fresh.
        del _failed[cluster_id]
        return False


def known_unreachable(cluster_id: str) -> bool:
    """True while this worker's last word on the cluster is a failure, tripped or not.

    This outlives the TTL. A dead peer is retried only once the trip expires, so between trips
    is_tripped is False although nothing has connected since. Fleet reads use this to tell that
    peer apart from a worker that has not connected yet.
    """
    with _lock:
        return cluster_id in _failed or cluster_id in _last_failed


def last_failure(cluster_id: str) -> Optional[Tuple[str, str]]:
    """(reason, kind) of the failure behind known_unreachable, or None once a connect succeeded."""
    with _lock:
        return _last_verdict.get(cluster_id)


def get_reason(cluster_id: str) -> Optional[str]:
    """Reason recorded for a currently-tripped cluster, if any."""
    with _lock:
        entry = _failed.get(cluster_id)
        return entry[1] if entry else None


def get_kind(cluster_id: str) -> Optional[str]:
    """Failure kind recorded for a currently-tripped cluster, if any.

    KIND_UNAUTHORIZED means the host answered; KIND_OFFLINE means it did not.
    """
    with _lock:
        entry = _failed.get(cluster_id)
        return entry[2] if entry else None


def failure_times() -> Dict[str, float]:
    """Snapshot of cluster_id -> last failure epoch for every cluster not cleared since."""
    with _lock:
        return dict(_last_failed)


def forget_unregistered(registered: Iterable[str]) -> None:
    """Drop everything recorded for clusters outside the registry.

    _last_failed outlives the TTL, so a cluster deregistered while failing would otherwise cost
    breaker_sync one marker read per pass in this worker until a restart.
    """
    keep = set(registered)
    with _lock:
        for cluster_id in (set(_failed) | set(_last_failed) | set(_cleared_at)) - keep:
            _failed.pop(cluster_id, None)
            _last_failed.pop(cluster_id, None)
            _last_verdict.pop(cluster_id, None)
            _cleared_at.pop(cluster_id, None)
