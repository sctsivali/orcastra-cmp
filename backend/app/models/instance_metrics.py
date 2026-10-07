"""Live metrics for one running instance, as served to the instance dialog and console bar.

note: re-exported from app.models.schemas so existing imports keep working. It lives here so
the metrics contract can grow without growing schemas.py.
"""

from datetime import datetime, timezone
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from app.models.vm_status import VMStatus

InstanceMetricSection = Literal["cpu", "memory", "disk", "network", "processes"]
CpuSampleStatus = Literal["ready", "calculating", "unavailable", "not_applicable"]
GuestAgentStatus = Literal["not_applicable", "connected", "unreachable", "disabled"]


class InstanceMetrics(BaseModel):
    """Real-time instance metrics for monitoring."""
    instance_name: str = Field(..., description="Instance name")
    host: str = Field(..., description="Registered Cluster identifier")
    status: VMStatus = Field(..., description="Current instance status")
    cpu_usage_percent: float = Field(
        0.0,
        description=(
            "Busy share of the instance's effective vCPUs over the sample window, 0 to 100. "
            "Only meaningful when cpu_sample_status is ready"
        ),
    )
    cpu_busy_vcpus: Optional[float] = Field(
        None, description="vCPU-seconds used per second over the sample window"
    )
    cpu_effective_vcpus: Optional[int] = Field(
        None, description="vCPUs the percentage is measured against"
    )
    sample_window_seconds: Optional[float] = Field(
        None, description="Length of the window behind the CPU and network rates"
    )
    memory_used_bytes: int = Field(0, description="Memory currently used in bytes")
    memory_total_bytes: int = Field(0, description="Total memory available in bytes")
    memory_usage_percent: float = Field(0.0, description="Memory usage percentage")
    memory_available_bytes: Optional[int] = Field(
        None, description="Memory the guest can still allocate without swapping, in bytes"
    )
    memory_cache_bytes: Optional[int] = Field(
        None, description="Page cache the guest can reclaim, in bytes"
    )
    memory_includes_cache: bool = Field(
        True,
        description="Whether memory_used_bytes still counts reclaimable page cache",
    )
    disk_usage_bytes: Dict[str, int] = Field(default_factory=dict, description="Disk usage by device in bytes")
    disk_total_bytes: Dict[str, int] = Field(
        default_factory=dict, description="Disk size by device in bytes, where a quota is set"
    )
    disk_read_bytes_per_second: Optional[float] = Field(
        None, description="Block device reads in bytes per second over disk_io_window_seconds"
    )
    disk_write_bytes_per_second: Optional[float] = Field(
        None, description="Block device writes in bytes per second over disk_io_window_seconds"
    )
    disk_io_window_seconds: Optional[float] = Field(
        None, description="Length of the window behind the disk I/O rates"
    )
    disk_io_status: Literal["ready", "calculating", "unavailable"] = Field(
        "unavailable",
        description=(
            "Whether the disk I/O rates have two samples, are waiting for the second, or are not "
            "reported by the cluster"
        ),
    )
    network_rx_bytes: Dict[str, int] = Field(default_factory=dict, description="Network received bytes by interface")
    network_tx_bytes: Dict[str, int] = Field(default_factory=dict, description="Network transmitted bytes by interface")
    network_rx_bytes_per_second: Dict[str, float] = Field(
        default_factory=dict, description="Network receive rate by interface in bytes per second"
    )
    network_tx_bytes_per_second: Dict[str, float] = Field(
        default_factory=dict, description="Network transmit rate by interface in bytes per second"
    )
    processes: int = Field(0, description="Number of running processes")
    uptime_seconds: int = Field(0, description="Instance uptime in seconds")
    base_image: Optional[str] = Field(None, description="Base image used to create instance")
    ipv4_addresses: List[str] = Field(default_factory=list, description="IPv4 addresses")
    ipv6_addresses: List[str] = Field(default_factory=list, description="IPv6 addresses")
    pid: Optional[int] = Field(None, description="Process ID of the instance")
    guest_agent: GuestAgentStatus = Field(
        "not_applicable",
        description="Virtual machine guest agent state; not_applicable for containers",
    )
    cpu_sample_status: CpuSampleStatus = Field(
        "unavailable",
        description=(
            "Whether CPU percentage has two usable samples, is waiting for its second sample, "
            "was not reported, or does not apply while the instance is stopped"
        ),
    )
    unavailable_sections: List[InstanceMetricSection] = Field(
        default_factory=list,
        description=(
            "Metric sections omitted or incomplete in the LXD state response. An empty section "
            "or an explicit numeric zero is still an available reading"
        ),
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC time of the state read behind this sample",
    )
