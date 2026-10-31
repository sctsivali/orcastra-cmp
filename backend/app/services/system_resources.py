"""
Collector behind GET /system/resource-usage: two honest scopes.

`host` is the kernel's view of the machine, `container` is this deployment's cgroup scope
(limits, what counts against them, the uvicorn worker set); the legacy top-level blocks keep
every key and its meaning (the timestamp is now UTC-aware). Nothing sleeps on the request path: every percent is a delta
against the previous call held in a process-wide sampler, and every optional reader is
wrapped so a missing source is a null field, never a 500.
"""

import logging
import math
import os
import platform
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import psutil
except ImportError:  # pragma: no cover - the router turns this into the install hint
    psutil = None

from app.models.system_resources import (
    ContainerCPU,
    ContainerInfo,
    ContainerMemory,
    CPUInfo,
    DiskInfo,
    HostCpuTimes,
    HostFilesystem,
    HostInfo,
    MemoryInfo,
    NetworkInterfaceInfo,
    OSInfo,
    ProcessInfo,
    PythonInfo,
    SystemResourceUsage,
    WorkerInfo,
    WorkersInfo,
)
from app.services import cgroup

_log = logging.getLogger(__name__)

# note: Docker bind-mounts these three files from the host filesystem; they are not places an
# operator stores anything, so they only clutter the filesystem list.
DOCKER_FILE_MOUNTS = frozenset({"/etc/hosts", "/etc/hostname", "/etc/resolv.conf"})
_HOST_BUSY_FIELDS = ("user", "nice", "system", "irq", "softirq", "steal")


class DeltaSampler:
    """Percent of a cumulative (busy, total) pair since the previous sample.

    Two polls inside the counter's resolution (two tabs, a refresh right after a poll) see no
    delta at all. Those keep the old baseline and answer with the last window rather than a
    false 0.0, so the next real poll still measures from the last point that moved.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._previous: Optional[Tuple[float, float, float]] = None
        self._last: Optional[Tuple[float, float]] = None

    def sample(self, busy: float, total: float) -> Optional[Tuple[float, float]]:
        now = time.monotonic()
        with self._lock:
            previous = self._previous
            if previous is None:
                self._previous = (now, busy, total)
                return None
            prev_time, prev_busy, prev_total = previous
            total_delta = total - prev_total
            if total_delta <= 0:
                return self._last
            percent = (busy - prev_busy) / total_delta * 100.0
            percent = min(100.0, max(0.0, percent))
            self._previous = (now, busy, total)
            self._last = (round(percent, 1), round(now - prev_time, 3))
            return self._last

    def reset(self) -> None:
        with self._lock:
            self._previous = None
            self._last = None


_HOST_CPU = DeltaSampler()
_CONTAINER_CPU = DeltaSampler()
_SELF_PROCESS: Optional[Any] = None


def _quiet(read: Callable[[], Any], default: Any = None) -> Any:
    """Run an optional reader; any failure becomes the default and a debug line."""
    try:
        return read()
    except Exception:
        _log.debug("%s unavailable", getattr(read, "__name__", "reader"), exc_info=True)
        return default


def _self_process() -> Any:
    # note: one Process object per worker so process.cpu_percent() has a baseline from the
    # previous call; re-created when the psutil module is swapped (tests).
    global _SELF_PROCESS
    if _SELF_PROCESS is None or not isinstance(_SELF_PROCESS, psutil.Process):
        _SELF_PROCESS = psutil.Process()
    return _SELF_PROCESS


def _host_cpu_reading() -> Tuple[HostCpuTimes, Optional[Tuple[float, float]]]:
    # important: psutil 7 keeps the cpu_percent(interval=None) baseline per thread and the
    # collector runs on a fresh asyncio.to_thread worker, so psutil's helper would answer 0.0
    # on every call; a positive interval would sleep on the request path instead. The busy
    # and total jiffies go through our own process-wide sampler.
    # note: that sampler is per worker, so its window is the gap since this worker last
    # answered, which can be minutes. The raw counters go out too, and the panel measures
    # its own poll-to-poll window from them whichever worker answers.
    times = psutil.cpu_times()
    busy = float(sum(getattr(times, field, 0.0) for field in _HOST_BUSY_FIELDS))
    total = busy + float(getattr(times, "idle", 0.0)) + float(getattr(times, "iowait", 0.0))
    return HostCpuTimes(busy_seconds=busy, total_seconds=total), _HOST_CPU.sample(busy, total)


def _container_cpu_percent(reading: cgroup.ContainerCpuReading, capacity: float) -> Optional[Tuple[float, float]]:
    if capacity <= 0:
        return None
    total_usec = time.monotonic() * 1_000_000 * capacity
    return _CONTAINER_CPU.sample(float(reading.usage_usec), total_usec)


def _prime_container_sampler() -> None:
    scope = cgroup.select_cpu_scope()
    reading = cgroup.read_cpu_usage(scope) if scope else None
    if reading is not None:
        _container_cpu_percent(reading, scope.limit or float(psutil.cpu_count(logical=True) or 1))


def _prime_samplers() -> None:
    if psutil is not None:
        _quiet(_host_cpu_reading)
        _quiet(_prime_container_sampler)


# --- Host scope ------------------------------------------------------------

def _collect_host_filesystems() -> List[HostFilesystem]:
    groups: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for partition in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(partition.mountpoint)
        except (PermissionError, OSError):
            continue
        key = (partition.device, usage.total)
        group = groups.get(key)
        if group is None:
            group = {"fstype": partition.fstype, "usage": usage, "raw": [], "kept": []}
            groups[key] = group
        group["raw"].append(partition.mountpoint)
        if partition.mountpoint not in DOCKER_FILE_MOUNTS:
            group["kept"].append(partition.mountpoint)

    filesystems: List[HostFilesystem] = []
    for (device, _total), group in groups.items():
        usage = group["usage"]
        filesystems.append(HostFilesystem(
            device=device,
            fstype=group["fstype"],
            mountpoints=group["kept"] or group["raw"][:1],
            total=usage.total,
            used=usage.used,
            free=usage.free,
            percent=usage.percent,
        ))
    return filesystems


def host_filesystems() -> List[HostFilesystem]:
    """The host's filesystems as the container sees them; [] without psutil. Host Health reads it."""
    if psutil is None:
        return []
    return _collect_host_filesystems()


def _host_cpu_info(sample: Optional[Tuple[float, float]]) -> CPUInfo:
    logical = psutil.cpu_count(logical=True) or 1
    physical = psutil.cpu_count(logical=False) or logical
    freq = _quiet(psutil.cpu_freq)
    load = _quiet(getattr(psutil, "getloadavg", lambda: (None, None, None)), (None, None, None))
    return CPUInfo(
        percent=sample[0] if sample else 0.0,
        count_logical=logical,
        count_physical=physical,
        frequency_current=freq.current if freq else None,
        frequency_max=freq.max if freq else None,
        load_avg_1m=load[0],
        load_avg_5m=load[1],
        load_avg_15m=load[2],
    )


def _collect_host() -> HostInfo:
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    boot_epoch = psutil.boot_time()
    is_vm, vendor = cgroup.detect_virtualization()
    cpu_times, sample = _host_cpu_reading()
    return HostInfo(
        cpu=_host_cpu_info(sample),
        memory=MemoryInfo(
            total=mem.total,
            available=mem.available,
            used=mem.used,
            percent=round(mem.percent, 1),
            swap_total=swap.total,
            swap_used=swap.used,
            swap_percent=swap.percent,
        ),
        filesystems=_quiet(_collect_host_filesystems, []),
        boot_time=datetime.fromtimestamp(boot_epoch, tz=timezone.utc).isoformat(),
        uptime_seconds=round(time.time() - boot_epoch, 1),
        is_virtual_machine=is_vm,
        platform_vendor=vendor,
        cpu_times=cpu_times,
        cpu_window_seconds=sample[1] if sample else None,
    )


# --- Container scope -------------------------------------------------------

def _process_exe(proc: Any) -> Optional[str]:
    for read in (proc.exe, proc.name):
        try:
            if value := read():
                return value
        except psutil.AccessDenied:
            continue
        except psutil.NoSuchProcess:
            return None
    return None


def _is_resource_tracker(proc: Any) -> bool:
    try:
        return any("resource_tracker" in arg for arg in proc.cmdline())
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        return False


def _expected_workers() -> Optional[int]:
    raw = os.environ.get("WEB_CONCURRENCY", "").strip()
    return int(raw) if raw.isdigit() else None


def _collect_workers() -> WorkersInfo:
    me = _self_process()
    my_exe = _process_exe(me)
    now = time.time()
    members: List[Any] = [me]
    master_pid: Optional[int] = None
    master_uptime: Optional[float] = None

    # note: under a uvicorn master the workers are our siblings, not every child of PID 1;
    # the multiprocessing resource_tracker shares the exe but serves no requests.
    parent = me.parent()
    if parent is not None and my_exe is not None and _process_exe(parent) == my_exe:
        siblings = [
            child for child in parent.children()
            if _process_exe(child) == my_exe and not _is_resource_tracker(child)
        ]
        if siblings:
            members = siblings
        master_pid = parent.pid
        try:
            master_uptime = round(now - parent.create_time(), 1)
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            master_uptime = None

    workers: List[WorkerInfo] = []
    for proc in members:
        try:
            with proc.oneshot():
                rss = proc.memory_info().rss
                uptime = now - proc.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        workers.append(WorkerInfo(pid=proc.pid, memory_rss=rss, uptime_seconds=round(uptime, 1)))

    return WorkersInfo(
        count=len(workers),
        expected=_expected_workers(),
        answering_pid=me.pid,
        master_pid=master_pid,
        master_uptime_seconds=master_uptime,
        total_rss=sum(worker.memory_rss for worker in workers),
        workers=workers,
    )


def _container_memory(scope: Optional[cgroup.MemoryScope], containerised: bool) -> Optional[ContainerMemory]:
    if scope is None or (scope.limit is None and not containerised):
        return None
    reading = cgroup.read_memory_usage(scope)
    if reading is None:
        return ContainerMemory(limit=scope.limit)
    # note: docker stats subtracts inactive_file, the page cache the kernel can reclaim
    # without touching the workload, so that is the figure a limit is compared against.
    used = reading.current - reading.inactive_file if reading.inactive_file is not None else None
    percent = round(used / scope.limit * 100, 1) if used is not None and scope.limit else None
    return ContainerMemory(
        limit=scope.limit,
        current=reading.current,
        anon=reading.anon,
        file=reading.file,
        used=used,
        percent=percent,
    )


def _container_cpu(scope: Optional[cgroup.CpuScope], containerised: bool, host_logical: int) -> Optional[ContainerCPU]:
    if scope is None or (scope.limit is None and not containerised):
        return None
    reading = cgroup.read_cpu_usage(scope)
    if reading is None:
        return ContainerCPU(limit=scope.limit)
    sample = _container_cpu_percent(reading, scope.limit or float(host_logical))
    return ContainerCPU(
        limit=scope.limit,
        usage_usec=reading.usage_usec,
        percent=sample[0] if sample else None,
        window_seconds=sample[1] if sample else None,
        nr_throttled=reading.nr_throttled,
        throttled_usec=reading.throttled_usec,
    )


def _collect_container(meta: Dict[str, Any], host_logical: int) -> ContainerInfo:
    containerised = bool(meta["is_containerized"])
    memory_scope = cgroup.select_memory_scope()
    cpu_scope = cgroup.select_cpu_scope()
    return ContainerInfo(
        is_containerized=containerised,
        runtime=meta["runtime"],
        cgroup_version=meta["cgroup_version"],
        memory_limit=memory_scope.limit if memory_scope else None,
        cpu_limit=cpu_scope.limit if cpu_scope else None,
        container_id=platform.node() if containerised else None,
        memory=_container_memory(memory_scope, containerised),
        cpu=_container_cpu(cpu_scope, containerised, host_logical),
        workers=_quiet(_collect_workers),
    )


# --- Legacy top-level blocks, assembled as before --------------------------

def _deduplicate_disks(disks: List[DiskInfo]) -> List[DiskInfo]:
    """One entry per (device, total) pair, preferring the root mountpoint."""
    seen: Dict[tuple, DiskInfo] = {}
    for disk in disks:
        key = (disk.device, disk.total)
        if key not in seen or disk.mountpoint == "/":
            seen[key] = disk
    return list(seen.values())


def _collect_legacy_disks(containerised: bool) -> List[DiskInfo]:
    disks: List[DiskInfo] = []
    for partition in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(partition.mountpoint)
        except (PermissionError, OSError):
            continue
        disks.append(DiskInfo(
            device=partition.device,
            mountpoint=partition.mountpoint,
            fstype=partition.fstype,
            total=usage.total,
            used=usage.used,
            free=usage.free,
            percent=usage.percent,
        ))
    return _deduplicate_disks(disks) if containerised else disks


def _collect_network() -> List[NetworkInterfaceInfo]:
    net_io = psutil.net_io_counters(pernic=True)
    net_if_addrs = psutil.net_if_addrs()
    net_if_stats = psutil.net_if_stats()
    network: List[NetworkInterfaceInfo] = []
    for iface, io in net_io.items():
        addrs = [
            addr.address for addr in net_if_addrs.get(iface, [])
            if addr.family.name in ("AF_INET", "AF_INET6")
        ]
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
    return network


def _collect_process(effective_total: int) -> Optional[ProcessInfo]:
    try:
        proc = _self_process()
        with proc.oneshot():
            mem_info = proc.memory_info()
            create_time = datetime.fromtimestamp(proc.create_time())
            uptime = (datetime.now() - create_time).total_seconds()
            io_read_bytes = None
            io_write_bytes = None
            try:
                io_counters = proc.io_counters()
                io_read_bytes = io_counters.read_bytes
                io_write_bytes = io_counters.write_bytes
            except (psutil.AccessDenied, AttributeError):
                pass
            connections = getattr(proc, "net_connections", None) or proc.connections
            return ProcessInfo(
                pid=proc.pid,
                cpu_percent=proc.cpu_percent(),
                memory_percent=round(mem_info.rss / effective_total * 100, 2) if effective_total > 0 else proc.memory_percent(),
                memory_rss=mem_info.rss,
                memory_vms=mem_info.vms,
                threads=proc.num_threads(),
                open_files=len(proc.open_files()),
                connections=len(connections()),
                create_time=create_time.isoformat(),
                uptime_seconds=uptime,
                io_read_bytes=io_read_bytes,
                io_write_bytes=io_write_bytes,
            )
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        return None


def _collect_legacy(container: ContainerInfo, host: HostInfo) -> Dict[str, Any]:
    cgroup_cpu_limit = container.cpu_limit
    effective_logical = max(1, math.ceil(cgroup_cpu_limit)) if cgroup_cpu_limit else host.cpu.count_logical
    effective_physical = min(psutil.cpu_count(logical=False) or effective_logical, effective_logical)
    cpu_info = host.cpu.model_copy(update={
        "count_logical": effective_logical,
        "count_physical": effective_physical,
    })

    mem = host.memory
    cgroup_mem_limit = container.memory_limit
    cgroup_mem_usage = container.memory.current if container.memory else None
    if cgroup_mem_limit is not None:
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
        swap_total=mem.swap_total,
        swap_used=mem.swap_used,
        swap_percent=mem.swap_percent,
    )

    boot_time = datetime.fromtimestamp(psutil.boot_time())
    os_info = OSInfo(
        system=platform.system(),
        release=platform.release(),
        version=platform.version(),
        machine=platform.machine(),
        hostname=platform.node(),
        boot_time=boot_time.isoformat(),
        uptime_seconds=(datetime.now() - boot_time).total_seconds(),
    )
    python_info = PythonInfo(
        version=platform.python_version(),
        implementation=platform.python_implementation(),
        compiler=platform.python_compiler(),
    )
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cpu": cpu_info,
        "memory": memory_info,
        "disks": _collect_legacy_disks(container.is_containerized),
        "network": _collect_network(),
        "process": _collect_process(effective_total),
        "python": python_info,
        "os": os_info,
    }


def collect_resource_usage() -> SystemResourceUsage:
    """Synchronous collector; the router runs it on a thread so nothing blocks the loop."""
    if psutil is None:
        raise ImportError("psutil is not installed. Install it with: pip install psutil")
    meta = cgroup.detect_container()
    host = _collect_host()
    container = _collect_container(meta, host.cpu.count_logical)
    legacy = _collect_legacy(container, host)
    return SystemResourceUsage(
        container=container,
        host=host,
        network_scope="container" if meta["is_containerized"] else "host",
        **legacy,
    )


_prime_samplers()
