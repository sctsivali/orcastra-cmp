"""One live metrics sample for one instance: the data behind the instance dialog and console bar.

Flow, all on one executor thread: find the instance in the authorized project, answer a stopped
instance at once, read state once and stamp the sample at the read's midpoint, fill the sections,
then turn the cumulative counters into rates through the shared sampler.

important: `build_state_sample` is the only place that names a sample's identity, epoch and
counter keys. The monitoring fleet sweep observes the same instances through it; two builders
that disagreed on any of those would re-seed each other's baseline on every poll and leave both
surfaces stuck on "calculating".
"""

import hashlib
import logging
import math
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import quote

from pylxd.exceptions import ClientConnectionFailed
from requests import exceptions as requests_exc

from app.core.rate_sampler import RateReading, RateSampler, Sample, SamplerPolicy
from app.models.instance_metrics import GuestAgentStatus, InstanceMetrics
from app.core.member_names import is_member_name, is_standalone_location

from .cpu_allocation import effective_vcpus
from .exceptions import LXDConnectionError, LXDOperationError
from .metrics_scrape import ProjectScrape, instance_scrape
from .metrics_text import InstanceSeries, honest_memory_for
from .instance_metrics_sections import (
    INSTANCE_METRIC_SECTIONS,
    apply_state_sections,
    base_image,
    cpu_usage_ns,
    guest_agent_status,
    state_mapping,
    uptime_seconds,
)
from .utils import find_instance_in_projects, parse_vm_status

logger = logging.getLogger(__name__)

STATE_SAMPLER = RateSampler(
    "instance_state", SamplerPolicy(min_window=1.5, max_age=120.0, ttl_seconds=180)
)
# note: a 10 s floor because the counters come from a scrape cached for 10 s; a shorter window
# would only ever compare a scrape with itself.
IO_SAMPLER = RateSampler(
    "instance_io", SamplerPolicy(min_window=10.0, max_age=180.0, ttl_seconds=240)
)

_HOST_THREADS_TTL_SECONDS = 1800.0
_HOST_THREADS_MAX_ENTRIES = 512
_host_threads_cache: "OrderedDict[str, Tuple[float, int]]" = OrderedDict()
_host_threads_lock = threading.Lock()
# note: bounds on what an LXD response can put into a Redis key or a stored sample.
_IDENTITY_PART_MAX = 200
_MAX_INTERFACES = 64


def _now() -> float:
    return time.time()


def _identity_part(part: Optional[str]) -> str:
    text = str(part or "")
    if len(text) > _IDENTITY_PART_MAX:
        # important: a real name never gets here; an endpoint inventing megabyte names must not
        # turn them into megabyte Redis keys. The digest keeps distinct values distinct.
        return "sha256-" + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    return quote(text, safe="")


def instance_rate_identity(
    cluster: str, project: str, name: str, uuid: Optional[str]
) -> str:
    """Sampler identity. The uuid makes a deleted and re-created same-name instance start fresh."""
    return ":".join(_identity_part(part) for part in (cluster, project, name, uuid))


def _start_marker(last_used_at: Any) -> str:
    """Whole seconds of the last start, the same for LXD's string and a parsed datetime.

    important: the dialog reads last_used_at as LXD's raw string ("...T00:00:00Z") and the fleet
    list carries it through VMInfo, where pydantic has already parsed it into a datetime. Put
    into the epoch as they come, the two never matched, and each surface re-seeded the other's
    baseline on every poll.
    """
    moment = last_used_at
    if isinstance(moment, str):
        text = moment.strip()[:64]
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return text
    if isinstance(moment, datetime):
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return str(int(moment.timestamp()))
    return ""


def build_state_sample(
    *,
    cluster: str,
    project: str,
    name: str,
    instance_type: Optional[str],
    config: Optional[Mapping[str, Any]],
    expanded_config: Optional[Mapping[str, Any]],
    last_used_at: Optional[str],
    state: Mapping[str, Any],
    taken_at: float,
) -> Tuple[str, Sample, GuestAgentStatus]:
    """Identity, counter sample and guest agent state for one state read."""
    agent = guest_agent_status(instance_type, expanded_config or config, state)
    counters: Dict[str, float] = {}
    cpu = cpu_usage_ns(state, agent)
    if cpu is not None:
        counters["cpu_ns"] = cpu
    network = state.get("network")
    if isinstance(network, Mapping):
        for iface in sorted(str(name) for name in network)[:_MAX_INTERFACES]:
            data = network.get(iface)
            if iface == "lo" or len(iface) > 64 or not isinstance(data, Mapping):
                continue
            values = data.get("counters")
            if not isinstance(values, Mapping):
                continue
            for key, field in (("net_rx", "bytes_received"), ("net_tx", "bytes_sent")):
                value = values.get(field)
                if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
                    counters[f"{key}:{iface}"] = float(value)
    # A restart or a move gives the instance a new init or QEMU pid and a new start time, and
    # every counter starts again from zero, so either one ends the baseline.
    pid = state.get("pid")
    epoch = f"{pid if isinstance(pid, int) else 0}:{_start_marker(last_used_at)}"
    identity = instance_rate_identity(
        cluster, project, name, (config or {}).get("volatile.uuid")
    )
    return identity, Sample(taken_at=taken_at, epoch=epoch, counters=counters), agent


def host_threads(client: Any, cluster: str, member: Optional[str]) -> Optional[int]:
    """Hardware threads of the host an unlimited container runs on, cached for 30 minutes."""
    standalone = is_standalone_location(member)
    if not standalone and not is_member_name(member):
        return None
    key = f"{cluster}\x00{'' if standalone else member}"
    now = time.monotonic()
    with _host_threads_lock:
        hit = _host_threads_cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
    params = {} if standalone else {"target": member}
    try:
        body = client.api.resources.get(params=params).json().get("metadata") or {}
        threads = int((body.get("cpu") or {}).get("total") or 0)
    except Exception as exc:  # noqa: BLE001 - a missing denominator is not a failed sample
        logger.debug("Host thread count unavailable for %s/%s: %s", cluster, member, exc)
        return None
    if threads <= 0:
        return None
    with _host_threads_lock:
        _host_threads_cache.pop(key, None)
        _host_threads_cache[key] = (now + _HOST_THREADS_TTL_SECONDS, threads)
        while len(_host_threads_cache) > _HOST_THREADS_MAX_ENTRIES:
            _host_threads_cache.popitem(last=False)
    return threads


def apply_rates(
    metrics: InstanceMetrics,
    reading: Optional[RateReading],
    *,
    cpu_counted: bool,
    vcpus: Optional[int],
) -> None:
    """CPU share and network rates from one sampler reading."""
    ready = reading is not None and reading.status == "ready"
    rates = reading.rates if ready and reading is not None else {}
    if ready and reading is not None:
        metrics.sample_window_seconds = reading.window_seconds
    if cpu_counted:
        busy_ns = rates.get("cpu_ns")
        if reading is None:
            metrics.cpu_sample_status = "unavailable"
        elif busy_ns is None:
            metrics.cpu_sample_status = "calculating"
        else:
            busy = busy_ns / 1e9
            if vcpus:
                # note: clamped to the allocation. Scheduler jitter between the two reads can
                # put a saturated 2-vCPU instance at 2.03 for one window.
                busy = min(busy, float(vcpus))
                metrics.cpu_usage_percent = round(busy / vcpus * 100, 2)
                metrics.cpu_sample_status = "ready"
            else:
                metrics.cpu_sample_status = "unavailable"
            metrics.cpu_busy_vcpus = round(busy, 3)
    for key, rate in rates.items():
        kind, _, iface = key.partition(":")
        if kind == "net_rx":
            metrics.network_rx_bytes_per_second[iface] = round(rate, 1)
        elif kind == "net_tx":
            metrics.network_tx_bytes_per_second[iface] = round(rate, 1)


def _observe(sampler: RateSampler, identity: str, sample: Sample) -> Optional[RateReading]:
    try:
        return sampler.observe(identity, sample)
    except Exception as exc:  # noqa: BLE001 - rates are optional, the sample is not
        logger.warning("Rate sampler %s failed for %s: %s", sampler.namespace, identity, exc)
        return None


def apply_honest_memory(
    metrics: InstanceMetrics, series: Optional[InstanceSeries], agent: GuestAgentStatus
) -> None:
    """Replace the state read's cache-inclusive memory with what the guest's programs hold."""
    if "memory" in metrics.unavailable_sections:
        return
    reading = honest_memory_for(series, agent)
    if reading is None:
        return
    metrics.memory_used_bytes = int(reading["used"] or 0)
    metrics.memory_total_bytes = int(reading["total"] or 0)
    metrics.memory_available_bytes = reading["available"]
    metrics.memory_cache_bytes = reading["cache"]
    metrics.memory_includes_cache = False
    if metrics.memory_total_bytes > 0:
        metrics.memory_usage_percent = round(
            metrics.memory_used_bytes / metrics.memory_total_bytes * 100, 2
        )


def apply_disk_io(
    metrics: InstanceMetrics,
    scrape: Optional[ProjectScrape],
    series: Optional[InstanceSeries],
    identity: str,
    epoch: str,
) -> None:
    """Disk I/O rates over a VM's whole disks, from two scrapes at least 10 s apart.

    important: virtual machines only, from their guest's own block devices. A container's disk
    counters come from its cgroup, which misses writes the storage driver does from kernel
    threads: on a ZFS pool a container writing 40 MB a second read 0 here. Showing that as a
    measured zero would be worse than saying nothing, so a container's disk I/O stays
    unavailable.
    """
    if scrape is None or series is None or series.instance_type != "virtual-machine":
        return
    counters: Dict[str, float] = {}
    if series.disk_read is not None:
        counters["disk_read"] = series.disk_read
    if series.disk_written is not None:
        counters["disk_write"] = series.disk_written
    if not counters:
        return
    reading = _observe(
        IO_SAMPLER, identity, Sample(taken_at=scrape.collected_at, epoch=epoch, counters=counters)
    )
    if reading is None:
        return
    if reading.status != "ready" or not reading.rates:
        metrics.disk_io_status = "calculating"
        return
    metrics.disk_io_status = "ready"
    metrics.disk_io_window_seconds = reading.window_seconds
    if "disk_read" in reading.rates:
        metrics.disk_read_bytes_per_second = round(reading.rates["disk_read"], 1)
    if "disk_write" in reading.rates:
        metrics.disk_write_bytes_per_second = round(reading.rates["disk_write"], 1)


def read_instance_metrics(
    client: Any,
    host: str,
    instance_name: str,
    project: str = "default",
    *,
    deadline: Optional[float] = None,
) -> InstanceMetrics:
    """One metrics sample. Blocking: runs on the instance metrics executor.

    `deadline` is a time.monotonic() value. Without one, the /1.0/metrics read is skipped and
    memory and disk come from the state read alone.
    """
    try:
        # important: scoped to the project the caller authorized. A name is unique per project,
        # so searching every project answered with whichever tenant's instance LXD listed first.
        instance, found_project = find_instance_in_projects(client, instance_name, project)
        if not instance:
            raise LXDOperationError(f"Instance '{instance_name}' not found on {host}")
        running = instance.status == "Running"
        metrics = InstanceMetrics(
            instance_name=instance_name,
            host=host,
            status=parse_vm_status(instance.status, instance.status_code),
            cpu_sample_status="unavailable" if running else "not_applicable",
        )
        if not running:
            return metrics

        started = _now()
        raw_state = instance.state()
        taken_at = (started + _now()) / 2
        metrics.timestamp = datetime.fromtimestamp(taken_at, timezone.utc)
        state = state_mapping(raw_state)
        if not state:
            metrics.unavailable_sections = list(INSTANCE_METRIC_SECTIONS)
            return metrics

        config = getattr(instance, "config", None) or {}
        expanded = getattr(instance, "expanded_config", None) or config
        last_used_at = getattr(instance, "last_used_at", None)
        identity, sample, agent = build_state_sample(
            cluster=host,
            project=found_project or project,
            name=instance_name,
            instance_type=getattr(instance, "type", None),
            config=config,
            expanded_config=expanded,
            last_used_at=last_used_at,
            state=state,
            taken_at=taken_at,
        )
        metrics.guest_agent = agent
        apply_state_sections(metrics, state, agent)
        metrics.uptime_seconds = uptime_seconds(state, last_used_at, metrics.timestamp)
        metrics.base_image = base_image(config)

        scrape = (
            instance_scrape(
                client,
                host,
                getattr(instance, "location", None),
                found_project or project,
                deadline=deadline,
            )
            if deadline is not None
            else None
        )
        series = scrape.series.get(instance_name) if scrape is not None else None
        apply_honest_memory(metrics, series, agent)

        cpu_counted = "cpu_ns" in sample.counters
        vcpus: Optional[int] = None
        if cpu_counted:
            reported = series.cpu_effective if series is not None else None
            vcpus = effective_vcpus(instance, reported=reported)
            if vcpus is None:
                vcpus = effective_vcpus(
                    instance,
                    host_threads=host_threads(client, host, getattr(instance, "location", None)),
                )
        metrics.cpu_effective_vcpus = vcpus
        apply_rates(
            metrics,
            _observe(STATE_SAMPLER, identity, sample),
            cpu_counted=cpu_counted,
            vcpus=vcpus,
        )
        if agent == "connected":
            apply_disk_io(metrics, scrape, series, identity, sample.epoch)
        return metrics
    except LXDOperationError:
        raise
    except (ClientConnectionFailed, requests_exc.ConnectionError, requests_exc.Timeout) as exc:
        logger.warning(
            "Connection failed while reading metrics for %s on %s: %s", instance_name, host, exc
        )
        raise LXDConnectionError(f"Could not read metrics from {host}") from exc
    except Exception as exc:
        logger.error("Error getting metrics for '%s' on %s: %s", instance_name, host, exc)
        raise LXDOperationError(f"Failed to get metrics: {exc}") from exc
