"""Turn one LXD instance state read into the sections of an InstanceMetrics sample.

important: presence is the contract. A field LXD omitted is unavailable; an explicit zero or an
empty collection is a real reading. The pylxd-style StateWrapper fills omitted fields with `{}`
and `0`, so presence comes from its `has_field`, never from the value.
"""

import math
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

from app.models.instance_metrics import GuestAgentStatus, InstanceMetrics, InstanceMetricSection
from .utils import is_display_address

INSTANCE_METRIC_SECTIONS: tuple[InstanceMetricSection, ...] = (
    "cpu",
    "memory",
    "disk",
    "network",
    "processes",
)
_STATE_FIELDS = ("cpu", "memory", "disk", "network", "processes", "pid", "started_at")
_AGENT_SECTIONS = frozenset({"cpu", "memory", "processes"})


def state_mapping(state: Any) -> Dict[str, Any]:
    """The fields the state read carried, as a plain mapping."""
    if state is None:
        return {}
    if isinstance(state, Mapping):
        return {key: state[key] for key in _STATE_FIELDS if state.get(key) is not None}
    has_field = getattr(state, "has_field", None)
    present: Dict[str, Any] = {}
    for key in _STATE_FIELDS:
        value = getattr(state, key, None)
        if callable(has_field):
            if has_field(key) and value is not None:
                present[key] = value
        elif value is not None:
            present[key] = value
    return present


def guest_agent_status(
    instance_type: Optional[str],
    expanded_config: Optional[Mapping[str, Any]],
    state: Mapping[str, Any],
) -> GuestAgentStatus:
    """Whether a virtual machine's in-guest figures can be trusted.

    LXD renders processes as -1 and CPU and memory as zero when it cannot reach lxd-agent, and
    `security.agent.metrics=false` stops it asking. Network and disk still come from the host.
    """
    if instance_type != "virtual-machine":
        return "not_applicable"
    if str((expanded_config or {}).get("security.agent.metrics", "")).strip().lower() == "false":
        return "disabled"
    processes = state.get("processes")
    if isinstance(processes, (int, float)) and processes < 0:
        return "unreachable"
    return "connected"


def cpu_usage_ns(state: Mapping[str, Any], agent: GuestAgentStatus) -> Optional[float]:
    """The cumulative CPU counter, or None when it is absent or not the guest's own."""
    if agent in ("unreachable", "disabled"):
        return None
    cpu = state.get("cpu")
    usage = cpu.get("usage") if isinstance(cpu, Mapping) else None
    if not isinstance(usage, (int, float)) or isinstance(usage, bool):
        return None
    if not math.isfinite(usage) or usage < 0:
        return None
    return float(usage)


def apply_state_sections(
    metrics: InstanceMetrics, state: Mapping[str, Any], agent: GuestAgentStatus
) -> None:
    """Fill memory, disk, network and processes; record what was missing. CPU is the caller's."""
    unavailable: set[str] = set()
    agent_offline = agent in ("unreachable", "disabled")
    if agent_offline:
        unavailable.update(_AGENT_SECTIONS)
    if cpu_usage_ns(state, agent) is None:
        unavailable.add("cpu")

    memory = state.get("memory")
    if not isinstance(memory, Mapping):
        unavailable.add("memory")
    elif not agent_offline:
        used, total = memory.get("usage"), memory.get("total")
        used = used if isinstance(used, (int, float)) else None
        total = total if isinstance(total, (int, float)) else None
        if used is None or total is None:
            unavailable.add("memory")
        if used is not None:
            metrics.memory_used_bytes = int(used)
        if total is not None:
            metrics.memory_total_bytes = int(total)
        if metrics.memory_total_bytes > 0:
            metrics.memory_usage_percent = round(
                metrics.memory_used_bytes / metrics.memory_total_bytes * 100, 2
            )

    disk = state.get("disk")
    if not isinstance(disk, Mapping):
        unavailable.add("disk")
    else:
        for device, data in disk.items():
            usage = data.get("usage") if isinstance(data, Mapping) else None
            if not isinstance(usage, (int, float)):
                unavailable.add("disk")
                continue
            metrics.disk_usage_bytes[device] = int(usage)
            total = data.get("total")
            if isinstance(total, (int, float)) and total > 0:
                metrics.disk_total_bytes[device] = int(total)

    network = state.get("network")
    if not isinstance(network, Mapping):
        unavailable.add("network")
    else:
        for iface, data in network.items():
            if iface == "lo":
                continue
            if not isinstance(data, Mapping):
                unavailable.add("network")
                continue
            counters = data.get("counters")
            if not isinstance(counters, Mapping):
                counters = {}
            received, sent = counters.get("bytes_received"), counters.get("bytes_sent")
            if received is None or sent is None:
                unavailable.add("network")
            if received is not None:
                metrics.network_rx_bytes[iface] = int(received)
            if sent is not None:
                metrics.network_tx_bytes[iface] = int(sent)
            addresses = data.get("addresses")
            for address in addresses if isinstance(addresses, list) else []:
                if not isinstance(address, Mapping) or not is_display_address(address):
                    continue
                target = (
                    metrics.ipv4_addresses
                    if address.get("family") == "inet"
                    else metrics.ipv6_addresses
                )
                target.append(address.get("address"))

    processes = state.get("processes")
    if agent_offline or not isinstance(processes, (int, float)) or processes < 0:
        unavailable.add("processes")
    else:
        metrics.processes = int(processes)

    pid = state.get("pid")
    if isinstance(pid, int) and pid > 0:
        metrics.pid = pid

    metrics.unavailable_sections = [s for s in INSTANCE_METRIC_SECTIONS if s in unavailable]


def uptime_seconds(
    state: Mapping[str, Any], last_used_at: Optional[str], now: datetime
) -> int:
    """Seconds since start. LXD's state carries no start time, so last_used_at is the source."""
    started = state.get("started_at") or last_used_at
    if not started:
        return 0
    try:
        text = str(started)
        started_at = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return 0
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return max(0, int((now - started_at).total_seconds()))


def base_image(config: Optional[Mapping[str, Any]]) -> Optional[str]:
    config = config or {}
    return config.get("image.description") or config.get("volatile.base_image") or None

