"""
System settings and configuration endpoints.
"""

import time
import platform
from datetime import datetime, timedelta
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict, List, Any
from app.core.config import get_settings
from fastapi import Depends
from app.core.dependencies import require_tenant, require_admin, AuthenticatedUser

router = APIRouter(
    prefix="/system",
    tags=["system"],
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
            # Clear all namespaces
            for ns in ["vault", "vault_list", "lxd_data"]:
                cache.delete_pattern(ns, "*")
            return {"message": "Cleared all caches"}
    except Exception as e:
        return {
            "error": str(e)
        }


# ========================
# System Resource Usage Models
# ========================

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
    
    Returns comprehensive information about CPU, memory, disk, and network usage.
    """
    try:
        import psutil
        import os
        
        # CPU Information
        cpu_freq = psutil.cpu_freq()
        load_avg = psutil.getloadavg() if hasattr(psutil, 'getloadavg') else (None, None, None)
        
        cpu_info = CPUInfo(
            percent=psutil.cpu_percent(interval=0.1),
            count_logical=psutil.cpu_count(logical=True),
            count_physical=psutil.cpu_count(logical=False) or psutil.cpu_count(logical=True),
            frequency_current=cpu_freq.current if cpu_freq else None,
            frequency_max=cpu_freq.max if cpu_freq else None,
            load_avg_1m=load_avg[0],
            load_avg_5m=load_avg[1],
            load_avg_15m=load_avg[2],
        )
        
        # Memory Information
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        
        memory_info = MemoryInfo(
            total=mem.total,
            available=mem.available,
            used=mem.used,
            percent=mem.percent,
            swap_total=swap.total,
            swap_used=swap.used,
            swap_percent=swap.percent,
        )
        
        # Disk Information
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
                    memory_percent=proc.memory_percent(),
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
