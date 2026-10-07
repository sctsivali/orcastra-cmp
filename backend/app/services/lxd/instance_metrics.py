"""One live metrics sample for one instance: the data behind the instance dialog and console bar.

Flow, all on one executor thread: find the instance in the authorized project, answer a stopped
instance at once, read state once and stamp the sample at the read's midpoint, fill the sections,
then turn the cumulative counters into rates through the shared sampler.

important: `build_state_sample` is the only place that names a sample's identity, epoch and
counter keys. The monitoring fleet sweep observes the same instances through it; two builders
that disagreed on any of those would re-seed each other's baseline on every poll and leave both
surfaces stuck on "calculating".
"""

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import quote

from pylxd.exceptions import ClientConnectionFailed
from requests import exceptions as requests_exc

from app.core.rate_sampler import RateReading, RateSampler, Sample, SamplerPolicy
from app.models.instance_metrics import GuestAgentStatus, InstanceMetrics
from .cpu_allocation import STANDALONE_LOCATIONS, effective_vcpus
from .exceptions import LXDConnectionError, LXDOperationError
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

_HOST_THREADS_TTL_SECONDS = 1800.0
_host_threads_cache: Dict[str, Tuple[float, int]] = {}
_host_threads_lock = threading.Lock()


def _now() -> float:
    return time.time()


def instance_rate_identity(
    cluster: str, project: str, name: str, uuid: Optional[str]
) -> str:
    """Sampler identity. The uuid makes a deleted and re-created same-name instance start fresh."""
    return ":".join(quote(part or "", safe="") for part in (cluster, project, name, uuid))


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
        for iface, data in network.items():
            if iface == "lo":
                continue
            values = (data or {}).get("counters") or {}
            for key, field in (("net_rx", "bytes_received"), ("net_tx", "bytes_sent")):
                value = values.get(field)
                if isinstance(value, (int, float)) and value >= 0:
                    counters[f"{key}:{iface}"] = float(value)
    # A restart or a move gives the instance a new init or QEMU pid and a new start time, and
    # every counter starts again from zero, so either one ends the baseline.
    epoch = f"{state.get('pid') or 0}:{last_used_at or ''}"
    identity = instance_rate_identity(
        cluster, project, name, (config or {}).get("volatile.uuid")
    )
    return identity, Sample(taken_at=taken_at, epoch=epoch, counters=counters), agent


def host_threads(client: Any, cluster: str, member: Optional[str]) -> Optional[int]:
    """Hardware threads of the host an unlimited container runs on, cached for 30 minutes."""
    key = f"{cluster}\x00{member or ''}"
    now = time.monotonic()
    with _host_threads_lock:
        hit = _host_threads_cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
    params = {} if (member or "") in STANDALONE_LOCATIONS else {"target": member}
    try:
        body = client.api.resources.get(params=params).json().get("metadata") or {}
        threads = int((body.get("cpu") or {}).get("total") or 0)
    except Exception as exc:  # noqa: BLE001 - a missing denominator is not a failed sample
        logger.debug("Host thread count unavailable for %s/%s: %s", cluster, member, exc)
        return None
    if threads <= 0:
        return None
    with _host_threads_lock:
        _host_threads_cache[key] = (now + _HOST_THREADS_TTL_SECONDS, threads)
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


def _observe(identity: str, sample: Sample) -> Optional[RateReading]:
    try:
        return STATE_SAMPLER.observe(identity, sample)
    except Exception as exc:  # noqa: BLE001 - rates are optional, the sample is not
        logger.warning("Rate sampler failed for %s: %s", identity, exc)
        return None


def read_instance_metrics(
    client: Any, host: str, instance_name: str, project: str = "default"
) -> InstanceMetrics:
    """One metrics sample. Blocking: runs on the instance metrics executor."""
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

        cpu_counted = "cpu_ns" in sample.counters
        vcpus: Optional[int] = None
        if cpu_counted:
            vcpus = effective_vcpus(instance)
            if vcpus is None:
                vcpus = effective_vcpus(
                    instance,
                    host_threads=host_threads(client, host, getattr(instance, "location", None)),
                )
        metrics.cpu_effective_vcpus = vcpus
        apply_rates(metrics, _observe(identity, sample), cpu_counted=cpu_counted, vcpus=vcpus)
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
