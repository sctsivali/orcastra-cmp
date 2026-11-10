"""The monitoring chart's history: one point per dashboard collection, kept for 24 hours.

A window is a time span: it selects the points whose timestamps fall inside it, whatever the
collection cadence (about one point every 30 s), so it never counts points.

Network is a rate: points carry the fleet's bytes per second, and their per-Cluster entries carry
the same so a Postgres reload can rebuild the fleet figure. A sum of lifetime counters would
spike every time an instance entered the sample.

Memory is the hosts' memory in use, the figure the gauge and each Cluster card show, so the chart
under the gauge agrees with it. Instance memory would include page cache.

The points themselves are stored and read by services/monitoring_history_store.py.
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
# Shortest gap between two points of one scope. Well under a collection cycle, well over the
# few seconds that separate workers finishing the same sweep.
HISTORY_SLOT_SECONDS = 20
HISTORY_SLOT_PREFIX = "orcastra:monitoring_history_slot"


def history_key(scope_key: str) -> str:
    """Map a dashboard scope key to its matching history cache key."""
    if scope_key.startswith("dashboard:"):
        return f"monitoring:history:{scope_key.split(':', 1)[1]}"
    return f"monitoring:history:{scope_key}"


def claim_history_slot(cache: Any, key: str, *, now: float, local: Dict[str, float]) -> bool:
    """Whether this computation may append the scope's next point. One winner per slot.

    important: the slot is claimed through Redis, so several workers that build the same
    dashboard within a few seconds append one point between them instead of a copy each. `local`
    is the per-process fallback for when Redis is unavailable.
    """
    client = getattr(cache, "redis_client", None)
    if client is not None:
        try:
            return bool(
                client.set(f"{HISTORY_SLOT_PREFIX}:{key}", repr(now), nx=True, ex=HISTORY_SLOT_SECONDS)
            )
        except Exception as exc:  # noqa: BLE001 - a lost slot read costs a duplicate, not a gap
            logger.debug("History slot unavailable for %s: %s", key, exc)
    if now - local.get(key, 0.0) < HISTORY_SLOT_SECONDS:
        return False
    local[key] = now
    return True


def _sum_rates(values: Sequence[Optional[float]]) -> Optional[float]:
    present = [value for value in values if value is not None]
    return round(sum(present), 1) if present else None


def build_point(
    nodes: Sequence[ClusterMonitoringMetrics],
    usages: Sequence[InstanceUsage],
    *,
    fleet_cpu_percent: Optional[float],
    memory_usage_percent: float,
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
        per_node[node.node_name] = {
            "cpu_usage_percent": node.cpu_busy_percent,
            "memory_usage_percent": node.memory_usage_percent,
            "storage_usage_percent": node.storage_usage_percent,
            "network_rx_bytes_per_sec": _sum_rates([u.rx_bps for u in items]),
            "network_tx_bytes_per_sec": _sum_rates([u.tx_bps for u in items]),
            "running_instances": len(items),
            # Readers must use .get(); older points lack these keys.
            "gpu_total": node.gpu_total,
            "gpu_used": node.gpu_used,
        }
    return {
        "timestamp": (now or datetime.now(timezone.utc)).isoformat(),
        "cpu_usage_percent": fleet_cpu_percent,
        "memory_usage_percent": memory_usage_percent,
        "storage_usage_percent": storage_usage_percent,
        "network_rx_bytes": 0,
        "network_tx_bytes": 0,
        "network_rx_bytes_per_sec": _sum_rates([n["network_rx_bytes_per_sec"] for n in per_node.values()]),
        "network_tx_bytes_per_sec": _sum_rates([n["network_tx_bytes_per_sec"] for n in per_node.values()]),
        "disk_io_bytes": 0,
        "running_instances": running_instances,
        "nodes": per_node,
    }


def parse_timestamp(timestamp: Any) -> Optional[datetime]:
    """A point's timestamp as an aware datetime (naive reads as UTC), or None when unreadable."""
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


def node_rates(nodes: Any) -> Tuple[Optional[float], Optional[float]]:
    """Fleet rates rebuilt from a point's per-Cluster entries, for points read back from Postgres."""
    if not isinstance(nodes, dict):
        return None, None
    entries = [entry for entry in nodes.values() if isinstance(entry, dict)]
    return (
        _sum_rates([entry.get("network_rx_bytes_per_sec") for entry in entries]),
        _sum_rates([entry.get("network_tx_bytes_per_sec") for entry in entries]),
    )
