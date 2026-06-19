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
from typing import Dict, Optional, Tuple

# How long a cluster stays "tripped" after a failure before it is probed again.
DEFAULT_TTL = 60.0

_lock = threading.Lock()
# cluster_id -> (failed_at_epoch, reason)
_failed: Dict[str, Tuple[float, str]] = {}


def record_failure(cluster_id: str, reason: str = "") -> None:
    """Mark a cluster as failed so callers skip it until the TTL expires."""
    with _lock:
        _failed[cluster_id] = (time.time(), reason or "unreachable")


def clear(cluster_id: str) -> None:
    """Un-trip a cluster after a successful connect so recovery is immediate."""
    with _lock:
        _failed.pop(cluster_id, None)


def is_tripped(cluster_id: str, ttl: float = DEFAULT_TTL) -> bool:
    """True if the cluster failed within the last ``ttl`` seconds."""
    with _lock:
        entry = _failed.get(cluster_id)
        if not entry:
            return False
        if time.time() - entry[0] < ttl:
            return True
        # expired - evict so the next probe runs fresh
        del _failed[cluster_id]
        return False


def get_reason(cluster_id: str) -> Optional[str]:
    """Reason recorded for a currently-tripped cluster, if any."""
    with _lock:
        entry = _failed.get(cluster_id)
        return entry[1] if entry else None
