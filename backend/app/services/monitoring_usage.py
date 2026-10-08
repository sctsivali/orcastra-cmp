"""Live usage per running instance for the monitoring dashboard, and its per-Cluster rollup.

The fleet list (recursion=2) already carries every running instance's state, so usage comes
from it without a request per instance. CPU and network rates go through the sampler and the
sample builder the instance dialog uses, so the two surfaces read one baseline and agree.

important: the sweep observes with a 10 s minimum window. While a dialog polls an instance
every 5 s the sweep reads the rates that poll computed instead of moving the baseline with a
list read whose timestamp is only good to half the list's duration. With no dialog open the
sweep's own 30 s windows keep the baselines warm, so a dialog opened later answers at once.

note: Cluster CPU is busy hardware threads, not a vCPU-weighted figure. A fleet over-committed
four to one can have its vCPUs a quarter busy while every thread is saturated; threads are what
the allocation line beside it is measured against, so the two read together.
"""

import logging
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Mapping, Optional, Sequence, Tuple

from app.core.blocking import run_blocking
from app.core.rate_sampler import RateReading, Sample
from app.models.schemas import ClusterMonitoringMetrics, InstanceMonitoringSummary, VMInfo
from app.core.member_names import is_standalone_location
from app.services.lxd.cpu_allocation import effective_vcpus
from app.services.lxd.instance_metrics import STATE_SAMPLER, build_state_sample
from app.services.lxd.instance_metrics_sections import state_mapping

logger = logging.getLogger(__name__)

FLEET_MIN_WINDOW_SECONDS = 10.0
CONSUMERS_PER_RESOURCE = 20
_SAMPLER_TIMEOUT_SECONDS = 5.0
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="monitoring_rates")

CpuStatus = Literal["ready", "calculating", "unavailable"]


@dataclass(frozen=True)
class InstanceUsage:
    vm: VMInfo
    cpu_status: CpuStatus
    busy_vcpus: Optional[float]
    vcpus: Optional[int]
    cpu_percent: Optional[float]
    memory_bytes: int
    memory_limit_bytes: int
    disk_bytes: int
    rx_total: int
    tx_total: int
    rx_bps: Optional[float]
    tx_bps: Optional[float]

    @property
    def key(self) -> Tuple[str, str, str]:
        return (self.vm.host, self.vm.project or "default", self.vm.name)

    @property
    def network_bps(self) -> Optional[float]:
        if self.rx_bps is None and self.tx_bps is None:
            return None
        return (self.rx_bps or 0.0) + (self.tx_bps or 0.0)


def _count(value: Any) -> int:
    """A non-negative finite counter, or 0 for anything else an endpoint sends."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return int(value)
    return 0


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _state_totals(state: Mapping[str, Any]) -> Tuple[int, int, int, int, int]:
    """Memory used and limit, disk used, and lifetime network totals from one state block."""
    memory = _mapping(state.get("memory"))
    used = _count(memory.get("usage"))
    limit = _count(memory.get("total"))
    disk = sum(_count(_mapping(data).get("usage")) for data in _mapping(state.get("disk")).values())
    rx = tx = 0
    for iface, data in _mapping(state.get("network")).items():
        if iface == "lo":
            continue
        counters = _mapping(_mapping(data).get("counters"))
        rx += _count(counters.get("bytes_received"))
        tx += _count(counters.get("bytes_sent"))
    return used, limit, disk, rx, tx


def _vcpus(vm: VMInfo, host_threads: Mapping[str, int]) -> Optional[int]:
    expanded = {"limits.cpu": vm.effective_cpu_limit} if vm.effective_limits_known else None
    standalone = is_standalone_location(vm.location)
    return effective_vcpus(
        {"type": vm.type, "expanded_config": expanded, "config": vm.config or {}},
        host_threads=host_threads.get(vm.host) if standalone else None,
    )


def _rate_sum(rates: Mapping[str, float], prefix: str) -> Optional[float]:
    values = [rate for key, rate in rates.items() if key.startswith(prefix)]
    return round(sum(values), 1) if values else None


def _usage(
    vm: VMInfo,
    state: Optional[Mapping[str, Any]],
    sample: Optional[Sample],
    reading: Optional[RateReading],
    host_threads: Mapping[str, int],
) -> InstanceUsage:
    if state is None or sample is None:
        return InstanceUsage(vm, "unavailable", None, None, None, 0, 0, 0, 0, 0, None, None)
    used, limit, disk, rx, tx = _state_totals(state)
    rates = reading.rates if reading is not None and reading.status == "ready" else {}
    status: CpuStatus = "unavailable"
    busy: Optional[float] = None
    vcpus: Optional[int] = None
    percent: Optional[float] = None
    if "cpu_ns" in sample.counters and reading is not None:
        if "cpu_ns" not in rates:
            status = "calculating"
        else:
            status = "ready"
            busy = rates["cpu_ns"] / 1e9
            vcpus = _vcpus(vm, host_threads)
            if vcpus:
                busy = min(busy, float(vcpus))
                percent = round(busy / vcpus * 100, 2)
            busy = round(busy, 3)
    return InstanceUsage(
        vm,
        status,
        busy,
        vcpus,
        percent,
        used,
        limit,
        disk,
        rx,
        tx,
        _rate_sum(rates, "net_rx:"),
        _rate_sum(rates, "net_tx:"),
    )


async def read_instance_usage(
    running_vms: Sequence[VMInfo], host_threads: Mapping[str, int]
) -> List[InstanceUsage]:
    """Usage for every running instance; rows without live state report unavailable CPU."""
    prepared: List[Tuple[VMInfo, Optional[str], Optional[Sample], Optional[Dict[str, Any]]]] = []
    samples: Dict[str, Sample] = {}
    for vm in running_vms:
        raw = getattr(vm, "live_state", None)
        read_at = getattr(vm, "live_state_read_at", None)
        if not isinstance(raw, dict) or read_at is None:
            prepared.append((vm, None, None, None))
            continue
        try:
            state = state_mapping(raw)
            identity, sample, _agent = build_state_sample(
                cluster=vm.host,
                project=vm.project or "default",
                name=vm.name,
                instance_type=vm.type,
                config=vm.config or {},
                expanded_config=vm.config or {},
                last_used_at=vm.last_used_at,
                state=state,
                taken_at=read_at,
            )
        except Exception as exc:  # noqa: BLE001 - one malformed row must not fail the dashboard
            logger.warning("Skipping usage for %s/%s: %s", vm.host, str(vm.name)[:80], str(exc)[:200])
            prepared.append((vm, None, None, None))
            continue
        samples[identity] = sample
        prepared.append((vm, identity, sample, state))

    readings: Dict[str, RateReading] = {}
    if samples:
        try:
            readings = await run_blocking(
                STATE_SAMPLER.observe_many,
                samples,
                timeout=_SAMPLER_TIMEOUT_SECONDS,
                label="monitoring.rates",
                executor=_executor,
                min_window=FLEET_MIN_WINDOW_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - the dashboard still answers, CPU unavailable
            logger.warning("Monitoring rate sampling failed for %d instances: %s", len(samples), exc)
            readings = {}

    usages: List[InstanceUsage] = []
    for vm, identity, sample, state in prepared:
        try:
            usages.append(
                _usage(vm, state, sample, readings.get(identity) if identity else None, host_threads)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skipping usage for %s/%s: %s", vm.host, str(vm.name)[:80], str(exc)[:200])
            usages.append(_usage(vm, None, None, None, host_threads))
    return usages


@dataclass(frozen=True)
class FleetCpu:
    busy_threads: Optional[float]
    busy_percent: Optional[float]
    status: CpuStatus
    reporting_instances: int
    # Hardware threads of the Clusters that reported: the denominator of busy_percent.
    reporting_threads: int = 0


def rollup(nodes: Sequence[ClusterMonitoringMetrics], usages: Sequence[InstanceUsage]) -> FleetCpu:
    """Fill each online Cluster's busy-thread figures and return the fleet's.

    A Cluster that has running instances but no reading yet is left out of the fleet sums
    rather than counted as idle, which would read a Cluster mid-recovery as quiet.

    important: the fleet is ready only once a Cluster that runs something has a reading, or
    when nothing runs anywhere. An idle Cluster is a true 0, but on its own it made the whole
    fleet "ready" at 0.0% while every busy Cluster was still calculating: seen live on the first
    sweep after a restart, drawn as a dive to zero and persisted.
    """
    by_node: Dict[str, List[InstanceUsage]] = {}
    for usage in usages:
        by_node.setdefault(usage.vm.host, []).append(usage)
    busy_total = 0.0
    threads_total = 0
    reporting = 0
    online = False
    # Statuses of the Clusters that run something; an idle Cluster cannot vouch for the fleet.
    statuses: List[CpuStatus] = []
    for node in nodes:
        if not node.is_online:
            continue
        online = True
        items = by_node.get(node.node_name, [])
        ready = [u for u in items if u.cpu_status == "ready" and u.busy_vcpus is not None]
        if node.running_instances == 0:
            busy: Optional[float] = 0.0
            status: CpuStatus = "ready"
        elif ready:
            busy = sum(u.busy_vcpus or 0.0 for u in ready)
            status = "ready"
        elif any(u.cpu_status == "calculating" for u in items):
            busy, status = None, "calculating"
        else:
            busy, status = None, "unavailable"
        node.cpu_status = status
        node.cpu_reporting_instances = len(ready)
        if node.running_instances > 0:
            statuses.append(status)
        if busy is None:
            continue
        node.cpu_busy_threads = round(busy, 2)
        node.cpu_busy_percent = round(busy / node.cpu_total * 100, 2) if node.cpu_total > 0 else None
        if node.cpu_total > 0:
            busy_total += busy
            threads_total += node.cpu_total
            reporting += len(ready)
    if not statuses:
        fleet_status: CpuStatus = "ready" if online else "unavailable"
    elif "ready" in statuses:
        fleet_status = "ready"
    elif "calculating" in statuses:
        fleet_status = "calculating"
    else:
        fleet_status = "unavailable"
    if fleet_status != "ready" or threads_total <= 0:
        return FleetCpu(None, None, fleet_status, reporting)
    return FleetCpu(
        round(busy_total, 2),
        round(busy_total / threads_total * 100, 2),
        fleet_status,
        reporting,
        threads_total,
    )


def select_consumers(
    usages: Sequence[InstanceUsage], per_resource: int = CONSUMERS_PER_RESOURCE
) -> List[InstanceUsage]:
    """Per Cluster, the leaders of each resource; the union, so any filter ranks exactly.

    The old list took the top 20 by CPU and drew the storage and network rankings from those
    20, so the heaviest disk or network user went missing whenever it was idle on CPU.
    """
    by_node: Dict[str, List[InstanceUsage]] = {}
    for usage in usages:
        by_node.setdefault(usage.vm.host, []).append(usage)
    chosen: Dict[Tuple[str, str, str], InstanceUsage] = {}
    for items in by_node.values():
        rankings = (
            sorted((u for u in items if u.busy_vcpus is not None), key=lambda u: u.busy_vcpus or 0.0, reverse=True),
            sorted(items, key=lambda u: u.memory_bytes, reverse=True),
            sorted(items, key=lambda u: u.disk_bytes, reverse=True),
            sorted((u for u in items if u.network_bps is not None), key=lambda u: u.network_bps or 0.0, reverse=True),
        )
        for ranking in rankings:
            for usage in ranking[:per_resource]:
                chosen.setdefault(usage.key, usage)
    return list(chosen.values())


def summary(usage: InstanceUsage) -> InstanceMonitoringSummary:
    vm = usage.vm
    return InstanceMonitoringSummary(
        name=vm.name,
        host=vm.host,
        project=vm.project or "default",
        type=vm.type or "container",
        status="Running",
        cpu_usage=usage.cpu_percent,
        cpu_status=usage.cpu_status,
        cpu_busy_vcpus=usage.busy_vcpus,
        cpu_vcpus=usage.vcpus,
        memory_usage_bytes=usage.memory_bytes,
        memory_limit_bytes=usage.memory_limit_bytes,
        disk_usage_bytes=usage.disk_bytes,
        network_rx_bytes=usage.rx_total,
        network_tx_bytes=usage.tx_total,
        network_rx_bytes_per_sec=usage.rx_bps,
        network_tx_bytes_per_sec=usage.tx_bps,
    )


def legacy_top_lists(
    usages: Sequence[InstanceUsage], limit: int = CONSUMERS_PER_RESOURCE
) -> Tuple[List[InstanceMonitoringSummary], List[InstanceMonitoringSummary]]:
    """The pre-consumers lists, kept for tabs opened before a deploy.

    note: CPU entries carry a percentage only. A tab from before this change formats
    cpu_usage as a number, so an entry without one would break it mid-render.
    """
    cpu = sorted(
        (u for u in usages if u.cpu_percent is not None),
        key=lambda u: u.busy_vcpus or 0.0,
        reverse=True,
    )[:limit]
    memory = sorted(usages, key=lambda u: u.memory_bytes, reverse=True)[:limit]
    return [summary(u) for u in cpu], [summary(u) for u in memory]
