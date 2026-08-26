"""
System settings and configuration endpoints.
"""

import logging
import math
import platform
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.dependencies import require_tenant, require_admin, AuthenticatedUser

router = APIRouter(
    prefix="/system",
    tags=["System"],
    dependencies=[Depends(require_tenant())],
)


class SystemSettings(BaseModel):
    """System settings response model."""
    read_only_mode: bool
    debug: bool


class VersionInfo(BaseModel):
    """Comprehensive version information."""
    app_version: str
    build_date: Optional[str] = None
    build_commit: Optional[str] = None
    environment: str


@router.get("/version", response_model=VersionInfo)
async def get_version():
    """
    Get comprehensive version information for the entire application.
    
    Returns version details including app version, API version, build metadata.
    This endpoint is used by the frontend to display version information.
    """
    settings = get_settings()
    return VersionInfo(
        app_version=settings.app_version,
        build_date=getattr(settings, 'build_date', None),
        build_commit=getattr(settings, 'build_commit', None),
        environment="development" if settings.debug else "production"
    )


@router.get("/settings", response_model=SystemSettings)
async def get_system_settings():
    """
    Get current system settings.
    
    Returns information about the system configuration including
    whether read-only mode is enabled.
    """
    settings = get_settings()
    return SystemSettings(
        read_only_mode=settings.read_only_mode,
        debug=settings.debug
    )


@router.get("/cache/stats")
async def get_cache_stats():
    """
    Get cache statistics for monitoring performance.
    
    Returns hit/miss ratios and cache sizes for both Redis and in-memory caches.
    """
    try:
        from app.core.cache import get_cache_manager
        from app.core.encryption import get_encryption
        
        cache = get_cache_manager()
        encryption = get_encryption()
        
        stats = cache.get_stats()
        stats["encryption_enabled"] = encryption is not None
        stats["sensitive_namespaces"] = cache.sensitive_namespaces
        
        return {
            "enabled": True,
            "stats": stats
        }
    except Exception as e:
        return {
            "enabled": False,
            "error": str(e)
        }


@router.post("/cache/clear")
async def clear_cache(
    namespace: str = None,
    _rbac: AuthenticatedUser = Depends(require_admin()),
):
    """
    Clear cache for a specific namespace or all caches.
    """
    try:
        from app.core.cache import get_cache_manager
        cache = get_cache_manager()
        
        if namespace:
            cache.delete_pattern(namespace, "*")
            return {"message": f"Cleared cache for namespace: {namespace}"}
        else:
            # important: this list has to be the namespaces actually in use.
            # It previously named "lxd_data", which no code writes to, and
            # omitted "api_responses", which is where every cached list
            # response lives, so the one manual cache-clear lever in the
            # product did not clear the thing operators were clearing it for.
            cleared = ["api_responses", "lxd_resources", "monitoring_history", "tags", "vault", "vault_list"]
            for ns in cleared:
                cache.delete_pattern(ns, "*")
            return {"message": "Cleared all caches", "namespaces": cleared}
    except Exception as e:
        return {
            "error": str(e)
        }


# ========================
# Container / CGroup Detection Helpers
# ========================

_log = logging.getLogger(__name__)

CGROUP_ROOT = Path("/sys/fs/cgroup")
CGROUP_UNLIMITED_MEMORY = 1 << 62


def _read_cgroup_file(path: str | Path) -> Optional[str]:
    """Safely read a cgroup file, return None on error."""
    try:
        return Path(path).read_text().strip()
    except (FileNotFoundError, PermissionError, OSError):
        return None


def _parse_proc_cgroup() -> List[Tuple[List[str], str]]:
    """Parse /proc/self/cgroup into controller/path pairs."""
    content = _read_cgroup_file("/proc/self/cgroup") or ""
    entries: List[Tuple[List[str], str]] = []

    for line in content.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue

        controllers = [item for item in parts[1].split(",") if item]
        entries.append((controllers, parts[2]))

    return entries


def _safe_cgroup_path(root: Path, raw_path: str) -> Path:
    """Build a cgroup path without allowing proc values to escape root."""
    parts = [part for part in raw_path.strip("/").split("/") if part not in ("", ".", "..")]
    return root.joinpath(*parts) if parts else root


def _ancestor_files(current: Path, root: Path, filename: str) -> List[Path]:
    """Return candidate files from current cgroup up to the cgroup root."""
    candidates: List[Path] = []
    cursor = current

    while True:
        candidates.append(cursor / filename)
        if cursor == root or cursor == cursor.parent:
            break
        cursor = cursor.parent

    return candidates


def _deduplicate_paths(paths: List[Path]) -> List[Path]:
    """Preserve path order while removing duplicates."""
    unique_paths: List[Path] = []
    seen: set[str] = set()

    for path in paths:
        path_key = str(path)
        if path_key in seen:
            continue
        seen.add(path_key)
        unique_paths.append(path)

    return unique_paths


def _candidate_cgroup_v2_files(filename: str) -> List[Path]:
    """Find cgroup v2 files for current and parent scopes."""
    candidates = [CGROUP_ROOT / filename]

    for controllers, raw_path in _parse_proc_cgroup():
        if controllers:
            continue
        current = _safe_cgroup_path(CGROUP_ROOT, raw_path)
        candidates.extend(_ancestor_files(current, CGROUP_ROOT, filename))

    return _deduplicate_paths(candidates)


def _candidate_cgroup_v1_files(controller: str, filename: str) -> List[Path]:
    """Find cgroup v1 files for current and parent scopes."""
    root = CGROUP_ROOT / controller
    candidates = [root / filename]

    for controllers, raw_path in _parse_proc_cgroup():
        if controller not in controllers:
            continue
        current = _safe_cgroup_path(root, raw_path)
        candidates.extend(_ancestor_files(current, root, filename))

    return _deduplicate_paths(candidates)


def _parse_int(value: Optional[str]) -> Optional[int]:
    """Parse a positive integer cgroup value."""
    if not value or value == "max":
        return None

    try:
        parsed = int(value)
    except ValueError:
        return None

    return parsed if parsed > 0 else None


def _parse_memory_limit(value: Optional[str]) -> Optional[int]:
    """Parse memory limit values and ignore unlimited cgroup sentinels."""
    limit = _parse_int(value)
    if limit is None or limit >= CGROUP_UNLIMITED_MEMORY:
        return None
    return limit


def _select_deployment_memory_scope() -> Tuple[Optional[int], Optional[int]]:
    """Return the outermost constrained memory scope for nested deployments."""
    scopes: List[Tuple[int, Optional[int]]] = []

    for limit_path in _candidate_cgroup_v2_files("memory.max"):
        limit = _parse_memory_limit(_read_cgroup_file(limit_path))
        if limit is None:
            continue
        usage = _parse_int(_read_cgroup_file(limit_path.with_name("memory.current")))
        scopes.append((limit, usage))

    for limit_path in _candidate_cgroup_v1_files("memory", "memory.limit_in_bytes"):
        limit = _parse_memory_limit(_read_cgroup_file(limit_path))
        if limit is None:
            continue
        usage = _parse_int(_read_cgroup_file(limit_path.with_name("memory.usage_in_bytes")))
        scopes.append((limit, usage))

    if not scopes:
        return None, None

    # note: largest constrained scope matches the deployment instance when Docker runs inside LXD/VMs.
    return max(scopes, key=lambda item: item[0])


def _detect_container() -> Dict[str, Any]:
    """Detect if running inside a container and return runtime info."""
    runtime = "bare-metal"
    is_containerized = False
    cgroup_version = None

    # detect cgroup version
    if Path("/sys/fs/cgroup/cgroup.controllers").exists():
        cgroup_version = "v2"
    elif Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").exists():
        cgroup_version = "v1"

    # detect Docker
    if Path("/.dockerenv").exists():
        runtime = "docker"
        is_containerized = True
    elif _read_cgroup_file("/proc/1/cgroup"):
        cgroup_content = _read_cgroup_file("/proc/1/cgroup") or ""
        if "docker" in cgroup_content or "containerd" in cgroup_content:
            runtime = "docker"
            is_containerized = True
        elif "lxc" in cgroup_content:
            runtime = "lxc"
            is_containerized = True

    # detect LXD/LXC via environment
    import os
    if os.environ.get("container") == "lxc":
        runtime = "lxc"
        is_containerized = True

    return {
        "is_containerized": is_containerized,
        "runtime": runtime,
        "cgroup_version": cgroup_version,
    }


def _get_cgroup_memory_limit() -> Optional[int]:
    """Read deployment memory limit from cgroup. Returns bytes or None."""
    limit, _usage = _select_deployment_memory_scope()
    return limit


def _get_cgroup_memory_usage() -> Optional[int]:
    """Read deployment memory usage from cgroup. Returns bytes or None."""
    _limit, usage = _select_deployment_memory_scope()
    if usage is not None:
        return usage

    val = _read_cgroup_file(CGROUP_ROOT / "memory.current")
    if val:
        return _parse_int(val)

    val = _read_cgroup_file(CGROUP_ROOT / "memory" / "memory.usage_in_bytes")
    return _parse_int(val)


def _parse_cpu_limit(value: Optional[str]) -> Optional[float]:
    """Parse cgroup v2 CPU quota as number of CPUs."""
    if not value:
        return None

    parts = value.split()
    if len(parts) != 2 or parts[0] == "max":
        return None

    try:
        quota = int(parts[0])
        period = int(parts[1])
    except ValueError:
        return None

    if quota <= 0 or period <= 0:
        return None

    return quota / period


def _parse_cpu_quota(quota_value: Optional[str], period_value: Optional[str]) -> Optional[float]:
    """Parse cgroup v1 CPU quota as number of CPUs."""
    quota = _parse_int(quota_value)
    period = _parse_int(period_value)

    if quota is None or period is None:
        return None

    return quota / period


def _get_cgroup_cpu_limit() -> Optional[float]:
    """Read deployment CPU limit from cgroup. Returns number of CPUs (float) or None."""
    limits: List[float] = []

    for cpu_path in _candidate_cgroup_v2_files("cpu.max"):
        limit = _parse_cpu_limit(_read_cgroup_file(cpu_path))
        if limit is not None:
            limits.append(limit)

    for quota_path in _candidate_cgroup_v1_files("cpu", "cpu.cfs_quota_us"):
        limit = _parse_cpu_quota(
            _read_cgroup_file(quota_path),
            _read_cgroup_file(quota_path.with_name("cpu.cfs_period_us")),
        )
        if limit is not None:
            limits.append(limit)

    return max(limits) if limits else None


def _deduplicate_disks(disks: list) -> list:
    """Deduplicate disk partitions that share the same backing device/pool.
    
    In containerized environments (Docker + ZFS/overlay), multiple mounts
    often point to the same underlying filesystem, showing identical total/used.
    Keep only one entry per unique (device, total) pair, preferring root (/).
    """
    seen: Dict[tuple, Any] = {}
    for disk in disks:
        key = (disk.device, disk.total)
        if key not in seen:
            seen[key] = disk
        else:
            # prefer root mountpoint
            if disk.mountpoint == "/":
                seen[key] = disk
    return list(seen.values())


# ========================
# System Resource Usage Models
# ========================

class ContainerInfo(BaseModel):
    """Container runtime information."""
    is_containerized: bool
    runtime: str  # docker, lxc, bare-metal
    cgroup_version: Optional[str] = None
    memory_limit: Optional[int] = None  # bytes, None if unlimited
    cpu_limit: Optional[float] = None   # number of CPUs, None if unlimited


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


class SystemResourceUsage(BaseModel):
    """Comprehensive system resource usage response."""
    timestamp: str
    container: Optional[ContainerInfo] = None
    cpu: CPUInfo
    memory: MemoryInfo
    disks: List[DiskInfo]
    network: List[NetworkInterfaceInfo]
    process: Optional[ProcessInfo] = None
    python: PythonInfo
    os: OSInfo


@router.get("/resource-usage", response_model=SystemResourceUsage)
async def get_system_resource_usage():
    """
    Get real-time system resource usage statistics.
    
    Deployment-aware: when running inside nested Docker/LXC, reports the
    constrained deployment scope instead of leaking bare-metal totals.
    """
    try:
        import psutil
        import os

        # Detect container environment and cgroup limits
        container_meta = _detect_container()
        cgroup_mem_limit = _get_cgroup_memory_limit()
        cgroup_mem_usage = _get_cgroup_memory_usage()
        cgroup_cpu_limit = _get_cgroup_cpu_limit()

        container_info = ContainerInfo(
            is_containerized=container_meta["is_containerized"],
            runtime=container_meta["runtime"],
            cgroup_version=container_meta["cgroup_version"],
            memory_limit=cgroup_mem_limit,
            cpu_limit=cgroup_cpu_limit,
        )

        # CPU Information - use cgroup CPU limit if available
        cpu_freq = psutil.cpu_freq()
        load_avg = psutil.getloadavg() if hasattr(psutil, 'getloadavg') else (None, None, None)

        host_logical = psutil.cpu_count(logical=True) or 1
        effective_logical = max(1, math.ceil(cgroup_cpu_limit)) if cgroup_cpu_limit else host_logical
        effective_physical = min(
            psutil.cpu_count(logical=False) or effective_logical,
            effective_logical,
        )
        
        cpu_info = CPUInfo(
            percent=psutil.cpu_percent(interval=0.1),
            count_logical=effective_logical,
            count_physical=effective_physical,
            frequency_current=cpu_freq.current if cpu_freq else None,
            frequency_max=cpu_freq.max if cpu_freq else None,
            load_avg_1m=load_avg[0],
            load_avg_5m=load_avg[1],
            load_avg_15m=load_avg[2],
        )
        
        # Memory Information - use cgroup memory limit if available
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()

        if cgroup_mem_limit is not None:
            # Use cgroup values for nested deployment scopes when available.
            effective_total = cgroup_mem_limit
            effective_used = cgroup_mem_usage if cgroup_mem_usage is not None else mem.used
            effective_available = max(effective_total - effective_used, 0)
            effective_percent = (effective_used / effective_total * 100) if effective_total > 0 else 0.0
        else:
            effective_total = mem.total
            effective_used = mem.used
            effective_available = mem.available
            effective_percent = mem.percent

        memory_info = MemoryInfo(
            total=effective_total,
            available=effective_available,
            used=effective_used,
            percent=round(effective_percent, 1),
            swap_total=swap.total,
            swap_used=swap.used,
            swap_percent=swap.percent,
        )
        
        # Disk Information - deduplicate overlapping container mounts
        disks = []
        for partition in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(partition.mountpoint)
                disks.append(DiskInfo(
                    device=partition.device,
                    mountpoint=partition.mountpoint,
                    fstype=partition.fstype,
                    total=usage.total,
                    used=usage.used,
                    free=usage.free,
                    percent=usage.percent,
                ))
            except (PermissionError, OSError):
                continue

        if container_info.is_containerized:
            disks = _deduplicate_disks(disks)
        
        # Network Information
        net_io = psutil.net_io_counters(pernic=True)
        net_if_addrs = psutil.net_if_addrs()
        net_if_stats = psutil.net_if_stats()
        
        network = []
        for iface, io in net_io.items():
            addrs = []
            if iface in net_if_addrs:
                for addr in net_if_addrs[iface]:
                    if addr.family.name in ('AF_INET', 'AF_INET6'):
                        addrs.append(addr.address)
            
            stats = net_if_stats.get(iface)
            network.append(NetworkInterfaceInfo(
                name=iface,
                bytes_sent=io.bytes_sent,
                bytes_recv=io.bytes_recv,
                packets_sent=io.packets_sent,
                packets_recv=io.packets_recv,
                is_up=stats.isup if stats else False,
                speed=stats.speed if stats else None,
                addresses=addrs,
            ))
        
        # Current Process Information
        process_info = None
        try:
            proc = psutil.Process(os.getpid())
            with proc.oneshot():
                mem_info = proc.memory_info()
                create_time = datetime.fromtimestamp(proc.create_time())
                uptime = (datetime.now() - create_time).total_seconds()
                
                # Get IO counters (may not be available on all platforms)
                io_read_bytes = None
                io_write_bytes = None
                try:
                    io_counters = proc.io_counters()
                    io_read_bytes = io_counters.read_bytes
                    io_write_bytes = io_counters.write_bytes
                except (psutil.AccessDenied, AttributeError):
                    pass
                
                process_info = ProcessInfo(
                    pid=proc.pid,
                    cpu_percent=proc.cpu_percent(),
                    memory_percent=round(mem_info.rss / effective_total * 100, 2) if effective_total > 0 else proc.memory_percent(),
                    memory_rss=mem_info.rss,
                    memory_vms=mem_info.vms,
                    threads=proc.num_threads(),
                    open_files=len(proc.open_files()),
                    connections=len(proc.connections()),
                    create_time=create_time.isoformat(),
                    uptime_seconds=uptime,
                    io_read_bytes=io_read_bytes,
                    io_write_bytes=io_write_bytes,
                )
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass
        
        # Python Information
        python_info = PythonInfo(
            version=platform.python_version(),
            implementation=platform.python_implementation(),
            compiler=platform.python_compiler(),
        )
        
        # OS Information
        boot_time = datetime.fromtimestamp(psutil.boot_time())
        system_uptime = (datetime.now() - boot_time).total_seconds()
        
        os_info = OSInfo(
            system=platform.system(),
            release=platform.release(),
            version=platform.version(),
            machine=platform.machine(),
            hostname=platform.node(),
            boot_time=boot_time.isoformat(),
            uptime_seconds=system_uptime,
        )
        
        return SystemResourceUsage(
            timestamp=datetime.now().isoformat(),
            container=container_info,
            cpu=cpu_info,
            memory=memory_info,
            disks=disks,
            network=network,
            process=process_info,
            python=python_info,
            os=os_info,
        )
        
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="psutil is not installed. Please install it with: pip install psutil"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
