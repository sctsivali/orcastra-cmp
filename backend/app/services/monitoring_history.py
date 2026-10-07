"""The monitoring chart's history: one point per dashboard collection, kept for 24 hours.

important: windows are time, not point counts. The chart's range buttons assumed one point every
5 s while the collector writes one every 30 s or so, so "5m" showed about half an hour. A window
now selects the points whose timestamps fall inside it, whatever the cadence was.

note: network is a rate. A point used to carry the sum of every sampled instance's lifetime
counters, and an instance entering the sample added its whole history to that sum, which the
chart drew as a spike. Points now carry the fleet's bytes per second, and their per-Cluster
entries carry the same so a Postgres reload can rebuild the fleet figure.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.models.schemas import ClusterMonitoringMetrics
from app.services.monitoring_usage import InstanceUsage

logger = logging.getLogger(__name__)

HISTORY_NAMESPACE = "monitoring_history"
HISTORY_TTL_SECONDS = 86400
HISTORY_RETENTION = timedelta(hours=24)
HISTORY_MAX_POINTS = 8640
DEFAULT_WINDOW_SECONDS = 300


def history_key(scope_key: str) -> str:
    """Map a dashboard scope key to its matching history cache key."""
    if scope_key.startswith("dashboard:"):
        return f"monitoring:history:{scope_key.split(':', 1)[1]}"
    return f"monitoring:history:{scope_key}"


def _sum_rates(values: Sequence[Optional[float]]) -> Optional[float]:
    present = [value for value in values if value is not None]
    return round(sum(present), 1) if present else None


def build_point(
    nodes: Sequence[ClusterMonitoringMetrics],
    usages: Sequence[InstanceUsage],
    *,
    fleet_cpu_percent: Optional[float],
    total_memory: int,
    storage_usage_percent: float,
    running_instances: int,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """A history point from already scoped dashboard figures."""
    by_node: Dict[str, List[InstanceUsage]] = {}
    for usage in usages:
        by_node.setdefault(usage.vm.host, []).append(usage)
    per_node: Dict[str, Dict[str, Any]] = {}
    for node in nodes:
        items = by_node.get(node.node_name, [])
        memory = sum(u.memory_bytes for u in items)
        per_node[node.node_name] = {
            "cpu_usage_percent": node.cpu_busy_percent,
            "memory_usage_percent": (
                round(memory / node.memory_total_bytes * 100, 2) if node.memory_total_bytes > 0 else 0
            ),
            "storage_usage_percent": node.storage_usage_percent,
            "network_rx_bytes_per_sec": _sum_rates([u.rx_bps for u in items]),
            "network_tx_bytes_per_sec": _sum_rates([u.tx_bps for u in items]),
            "running_instances": len(items),
            # note: readers must use .get(), older points lack the keys.
            "gpu_total": node.gpu_total,
            "gpu_used": node.gpu_used,
        }
    memory_used = sum(u.memory_bytes for u in usages)
    return {
        "timestamp": (now or datetime.now(timezone.utc)).isoformat(),
        "cpu_usage_percent": fleet_cpu_percent,
        "memory_usage_percent": round(memory_used / total_memory * 100, 2) if total_memory > 0 else 0,
        "storage_usage_percent": storage_usage_percent,
        "network_rx_bytes": 0,
        "network_tx_bytes": 0,
        "network_rx_bytes_per_sec": _sum_rates([n["network_rx_bytes_per_sec"] for n in per_node.values()]),
        "network_tx_bytes_per_sec": _sum_rates([n["network_tx_bytes_per_sec"] for n in per_node.values()]),
        "disk_io_bytes": 0,
        "running_instances": running_instances,
        "nodes": per_node,
    }


def _parse(timestamp: Any) -> Optional[datetime]:
    if isinstance(timestamp, datetime):
        parsed = timestamp
    elif isinstance(timestamp, str):
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _first_at_or_after(history: Sequence[Dict[str, Any]], cutoff: datetime) -> int:
    for index, point in enumerate(history):
        stamp = _parse(point.get("timestamp"))
        if stamp is not None and stamp >= cutoff:
            return index
    return len(history)


def append_point(
    history: List[Dict[str, Any]], point: Dict[str, Any], *, now: Optional[datetime] = None
) -> List[Dict[str, Any]]:
    """Append and drop what fell out of the 24-hour window; the count cap stays as a backstop."""
    history.append(point)
    cutoff = (now or datetime.now(timezone.utc)) - HISTORY_RETENTION
    start = _first_at_or_after(history, cutoff)
    if start:
        history = history[start:]
    if len(history) > HISTORY_MAX_POINTS:
        history = history[-HISTORY_MAX_POINTS:]
    return history


def read_window(
    history: Sequence[Dict[str, Any]],
    *,
    window_seconds: Optional[int],
    points: Optional[int],
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """The points inside the window, or the last `points` for the legacy count parameter."""
    if window_seconds is None and points is not None:
        return list(history[-points:])
    seconds = window_seconds or DEFAULT_WINDOW_SECONDS
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(seconds=seconds)
    return list(history[_first_at_or_after(history, cutoff):])


def node_rates(nodes: Any) -> Tuple[Optional[float], Optional[float]]:
    """Fleet rates rebuilt from a point's per-Cluster entries, for points read back from Postgres."""
    if not isinstance(nodes, dict):
        return None, None
    entries = [entry for entry in nodes.values() if isinstance(entry, dict)]
    return (
        _sum_rates([entry.get("network_rx_bytes_per_sec") for entry in entries]),
        _sum_rates([entry.get("network_tx_bytes_per_sec") for entry in entries]),
    )
