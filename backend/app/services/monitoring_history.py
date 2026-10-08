"""The monitoring chart's history: one point per dashboard collection, kept for 24 hours.

important: windows are time, not point counts. The chart's range buttons assumed one point every
5 s while the collector writes one every 30 s or so, so "5m" showed about half an hour. A window
now selects the points whose timestamps fall inside it, whatever the cadence was.

note: network is a rate. A point used to carry the sum of every sampled instance's lifetime
counters, and an instance entering the sample added its whole history to that sum, which the
chart drew as a spike. Points now carry the fleet's bytes per second, and their per-Cluster
entries carry the same so a Postgres reload can rebuild the fleet figure.

note: memory is the hosts' memory in use, the figure the gauge and each Cluster card show. A
point used to sum the running instances' memory, page cache included, over the hosts' total,
so the chart under the gauge disagreed with it by the size of the cache.

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

    important: shared through Redis, not per process. Each of the four workers kept its own
    "last stored" time, so when several of them built the same dashboard within a few seconds
    every one of them appended it: over a day about 70% of the points were copies, and the
    read-modify-write of the history value let concurrent appends overwrite each other. `local`
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
            # note: readers must use .get(), older points lack the keys.
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
