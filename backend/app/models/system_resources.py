"""
Response models for GET /system/resource-usage.

The top-level cpu/memory/disks/process/os blocks predate the two-scope panel and mix the host
and the container view (a cgroup memory limit is reported as memory.total, a cpu quota as
cpu.count_logical). They are kept unchanged for older clients. New consumers read `host`
(the kernel's view of the machine) and `container` (this deployment's cgroup scope).
"""

from typing import List, Literal, Optional

from pydantic import BaseModel


class CPUInfo(BaseModel):
    """CPU usage information."""
    percent: float
    count_logical: int
    count_physical: int
    frequency_current: Optional[float] = None
    frequency_max: Optional[float] = None
    load_avg_1m: Optional[float] = None
    load_avg_5m: Optional[float] = None
    load_avg_15m: Optional[float] = None


class MemoryInfo(BaseModel):
    """Memory usage information."""
    total: int
    available: int
    used: int
    percent: float
    swap_total: int
    swap_used: int
    swap_percent: float


class DiskInfo(BaseModel):
    """Disk usage for a single partition."""
    device: str
    mountpoint: str
    fstype: str
    total: int
    used: int
    free: int
    percent: float


class NetworkInterfaceInfo(BaseModel):
    """Network interface statistics."""
    name: str
    bytes_sent: int
    bytes_recv: int
    packets_sent: int
    packets_recv: int
    is_up: bool
    speed: Optional[int] = None
    addresses: List[str] = []


class ProcessInfo(BaseModel):
    """Process information for this application."""
    pid: int
    cpu_percent: float
    memory_percent: float
    memory_rss: int
    memory_vms: int
    threads: int
    open_files: int
    connections: int
    create_time: str
    uptime_seconds: float
    io_read_bytes: Optional[int] = None
    io_write_bytes: Optional[int] = None


class PythonInfo(BaseModel):
    """Python runtime information."""
    version: str
    implementation: str
    compiler: str


class OSInfo(BaseModel):
    """Operating system information."""
    system: str
    release: str
    version: str
    machine: str
    hostname: str
    boot_time: str
    uptime_seconds: float


class ContainerMemory(BaseModel):
    """Memory of the deployment cgroup. `used` is current minus inactive_file (docker stats)."""
    limit: Optional[int] = None
    current: Optional[int] = None
    anon: Optional[int] = None
    file: Optional[int] = None
    used: Optional[int] = None
    percent: Optional[float] = None


class ContainerCPU(BaseModel):
    """CPU of the deployment cgroup; percent is of the quota over window_seconds."""
    limit: Optional[float] = None
    usage_usec: Optional[int] = None
    percent: Optional[float] = None
    window_seconds: Optional[float] = None
    nr_throttled: Optional[int] = None
    throttled_usec: Optional[int] = None


class WorkerInfo(BaseModel):
    pid: int
    memory_rss: int
    uptime_seconds: float


class WorkersInfo(BaseModel):
    """The uvicorn worker set this process belongs to."""
    count: int
    expected: Optional[int] = None
    answering_pid: int
    master_pid: Optional[int] = None
    master_uptime_seconds: Optional[float] = None
    total_rss: int
    workers: List[WorkerInfo] = []


class ContainerInfo(BaseModel):
    """Container runtime information."""
    is_containerized: bool
    runtime: str  # docker, lxc, bare-metal
    cgroup_version: Optional[str] = None
    memory_limit: Optional[int] = None  # bytes, None if unlimited
    cpu_limit: Optional[float] = None   # number of CPUs, None if unlimited
    container_id: Optional[str] = None
    memory: Optional[ContainerMemory] = None
    cpu: Optional[ContainerCPU] = None
    workers: Optional[WorkersInfo] = None


class HostFilesystem(BaseModel):
    """One host filesystem and every mountpoint it is visible at."""
    device: str
    fstype: str
    mountpoints: List[str]
    total: int
    used: int
    free: int
    percent: float


class HostInfo(BaseModel):
    """The kernel's view of the machine, unclamped by cgroup limits."""
    cpu: CPUInfo
    memory: MemoryInfo
    filesystems: List[HostFilesystem] = []
    boot_time: str
    uptime_seconds: float
    is_virtual_machine: Optional[bool] = None
    platform_vendor: Optional[str] = None


class SystemResourceUsage(BaseModel):
    """Comprehensive system resource usage response.

    The top-level cpu/memory/disks/process/os blocks are kept for older clients and mix
    scopes; new consumers read `host` and `container`.
    """
    timestamp: str
    container: Optional[ContainerInfo] = None
    cpu: CPUInfo
    memory: MemoryInfo
    disks: List[DiskInfo]
    network: List[NetworkInterfaceInfo]
    process: Optional[ProcessInfo] = None
    python: PythonInfo
    os: OSInfo
    host: Optional[HostInfo] = None
    network_scope: Literal["container", "host"] = "host"
