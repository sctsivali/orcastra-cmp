"""
cgroup and platform readers for the deployment scope of this process.

Everything here reads files under the module path constants so a test can point them at a
fake tree. Readers return None on any failure and never raise: a missing or unreadable file
is a null field in the resource panel, not a 500.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

CGROUP_ROOT = Path("/sys/fs/cgroup")
PROC_SELF_CGROUP = Path("/proc/self/cgroup")
PROC_1_CGROUP = Path("/proc/1/cgroup")
DOCKERENV = Path("/.dockerenv")
DMI_SYS_VENDOR = Path("/sys/class/dmi/id/sys_vendor")
PROC_CPUINFO = Path("/proc/cpuinfo")

CGROUP_UNLIMITED_MEMORY = 1 << 62

CgroupVersion = Literal["v1", "v2"]


@dataclass(frozen=True)
class MemoryScope:
    """The cgroup directory whose memory files describe this deployment."""
    path: Path
    version: CgroupVersion
    limit: Optional[int]


@dataclass(frozen=True)
class CpuScope:
    """The cgroup directory whose cpu files describe this deployment."""
    path: Path
    version: CgroupVersion
    limit: Optional[float]


@dataclass(frozen=True)
class ContainerMemoryReading:
    current: int
    anon: Optional[int]
    file: Optional[int]
    inactive_file: Optional[int]


@dataclass(frozen=True)
class ContainerCpuReading:
    usage_usec: int
    nr_throttled: Optional[int]
    throttled_usec: Optional[int]


# ---------------------------------------------------------------------------
# File and path helpers
# ---------------------------------------------------------------------------

def read_cgroup_file(path: str | Path) -> Optional[str]:
    try:
        return Path(path).read_text().strip()
    except (FileNotFoundError, PermissionError, OSError):
        return None


def _parse_proc_cgroup() -> List[Tuple[List[str], str]]:
    content = read_cgroup_file(PROC_SELF_CGROUP) or ""
    entries: List[Tuple[List[str], str]] = []
    for line in content.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        controllers = [item for item in parts[1].split(",") if item]
        entries.append((controllers, parts[2]))
    return entries


def _safe_cgroup_path(root: Path, raw_path: str) -> Path:
    # important: proc values are untrusted for path building; drop "." and ".." segments.
    parts = [part for part in raw_path.strip("/").split("/") if part not in ("", ".", "..")]
    return root.joinpath(*parts) if parts else root


def _ancestor_files(current: Path, root: Path, filename: str) -> List[Path]:
    candidates: List[Path] = []
    cursor = current
    while True:
        candidates.append(cursor / filename)
        if cursor == root or cursor == cursor.parent:
            break
        cursor = cursor.parent
    return candidates


def _deduplicate_paths(paths: List[Path]) -> List[Path]:
    unique_paths: List[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique_paths.append(path)
    return unique_paths


def _candidate_cgroup_v2_files(filename: str) -> List[Path]:
    candidates = [CGROUP_ROOT / filename]
    for controllers, raw_path in _parse_proc_cgroup():
        if controllers:
            continue
        current = _safe_cgroup_path(CGROUP_ROOT, raw_path)
        candidates.extend(_ancestor_files(current, CGROUP_ROOT, filename))
    return _deduplicate_paths(candidates)


def _candidate_cgroup_v1_files(controller: str, filename: str) -> List[Path]:
    root = CGROUP_ROOT / controller
    candidates = [root / filename]
    for controllers, raw_path in _parse_proc_cgroup():
        if controller not in controllers:
            continue
        current = _safe_cgroup_path(root, raw_path)
        candidates.extend(_ancestor_files(current, root, filename))
    return _deduplicate_paths(candidates)


def _sibling_controller_dir(path: Path, controller: str, sibling: str) -> Optional[Path]:
    # note: cgroup v1 keeps cpu quota under cpu/ and the usage counter under cpuacct/ at the same
    # relative path, so the scope directory is re-rooted onto the sibling controller.
    try:
        relative = path.relative_to(CGROUP_ROOT / controller)
    except ValueError:
        return None
    return CGROUP_ROOT / sibling / relative


# ---------------------------------------------------------------------------
# Value parsers
# ---------------------------------------------------------------------------

def parse_int(value: Optional[str]) -> Optional[int]:
    """Parse a positive integer cgroup value; "max" and zero mean unset."""
    if not value or value == "max":
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _parse_counter(value: Optional[str]) -> Optional[int]:
    """Parse a non-negative counter; unlike a limit, zero is a real reading."""
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _parse_stat(content: Optional[str]) -> Dict[str, int]:
    """Parse a "key value" per line file such as memory.stat or cpu.stat."""
    stats: Dict[str, int] = {}
    for line in (content or "").splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        parsed = _parse_counter(parts[1])
        if parsed is not None:
            stats[parts[0]] = parsed
    return stats


def parse_memory_limit(value: Optional[str]) -> Optional[int]:
    limit = parse_int(value)
    if limit is None or limit >= CGROUP_UNLIMITED_MEMORY:
        return None
    return limit


def parse_cpu_limit(value: Optional[str]) -> Optional[float]:
    """Parse cgroup v2 cpu.max ("quota period") as a number of CPUs."""
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


def parse_cpu_quota(quota_value: Optional[str], period_value: Optional[str]) -> Optional[float]:
    """Parse cgroup v1 cfs quota and period as a number of CPUs."""
    quota = parse_int(quota_value)
    period = parse_int(period_value)
    if quota is None or period is None:
        return None
    return quota / period


# ---------------------------------------------------------------------------
# Scope selection
# ---------------------------------------------------------------------------

def _first_readable_dir(candidates: List[Path], usage_filename: str) -> Optional[Path]:
    for candidate in candidates:
        if read_cgroup_file(candidate.with_name(usage_filename)) is not None:
            return candidate.parent
    return None


def select_memory_scope() -> Optional[MemoryScope]:
    """Largest constrained memory scope, else the nearest cgroup with a readable usage file."""
    scopes: List[MemoryScope] = []
    v2_candidates = _candidate_cgroup_v2_files("memory.max")
    v1_candidates = _candidate_cgroup_v1_files("memory", "memory.limit_in_bytes")

    for limit_path in v2_candidates:
        limit = parse_memory_limit(read_cgroup_file(limit_path))
        if limit is not None:
            scopes.append(MemoryScope(limit_path.parent, "v2", limit))
    for limit_path in v1_candidates:
        limit = parse_memory_limit(read_cgroup_file(limit_path))
        if limit is not None:
            scopes.append(MemoryScope(limit_path.parent, "v1", limit))

    if scopes:
        # note: largest constrained scope matches the deployment instance when Docker runs inside LXD/VMs.
        return max(scopes, key=lambda scope: scope.limit or 0)

    own = _first_readable_dir(v2_candidates, "memory.current")
    if own is not None:
        return MemoryScope(own, "v2", None)
    own = _first_readable_dir(v1_candidates, "memory.usage_in_bytes")
    if own is not None:
        return MemoryScope(own, "v1", None)
    return None


def select_cpu_scope() -> Optional[CpuScope]:
    """Largest constrained cpu scope, else the nearest cgroup with a readable usage counter."""
    scopes: List[CpuScope] = []
    v2_candidates = _candidate_cgroup_v2_files("cpu.max")
    v1_candidates = _candidate_cgroup_v1_files("cpu", "cpu.cfs_quota_us")

    for cpu_path in v2_candidates:
        limit = parse_cpu_limit(read_cgroup_file(cpu_path))
        if limit is not None:
            scopes.append(CpuScope(cpu_path.parent, "v2", limit))
    for quota_path in v1_candidates:
        limit = parse_cpu_quota(
            read_cgroup_file(quota_path),
            read_cgroup_file(quota_path.with_name("cpu.cfs_period_us")),
        )
        if limit is not None:
            scopes.append(CpuScope(quota_path.parent, "v1", limit))

    if scopes:
        return max(scopes, key=lambda scope: scope.limit or 0.0)

    own = _first_readable_dir(v2_candidates, "cpu.stat")
    if own is not None:
        return CpuScope(own, "v2", None)
    for quota_path in v1_candidates:
        usage_dir = _sibling_controller_dir(quota_path.parent, "cpu", "cpuacct")
        if usage_dir is not None and read_cgroup_file(usage_dir / "cpuacct.usage") is not None:
            return CpuScope(quota_path.parent, "v1", None)
    return None


# ---------------------------------------------------------------------------
# Usage readers
# ---------------------------------------------------------------------------

def read_memory_usage(scope: MemoryScope) -> Optional[ContainerMemoryReading]:
    try:
        if scope.version == "v2":
            current = _parse_counter(read_cgroup_file(scope.path / "memory.current"))
            stat = _parse_stat(read_cgroup_file(scope.path / "memory.stat"))
            keys = ("anon", "file", "inactive_file")
        else:
            current = _parse_counter(read_cgroup_file(scope.path / "memory.usage_in_bytes"))
            stat = _parse_stat(read_cgroup_file(scope.path / "memory.stat"))
            keys = ("total_rss", "total_cache", "total_inactive_file")
        if current is None:
            return None
        return ContainerMemoryReading(
            current=current,
            anon=stat.get(keys[0]),
            file=stat.get(keys[1]),
            inactive_file=stat.get(keys[2]),
        )
    except Exception:
        return None


def read_cpu_usage(scope: CpuScope) -> Optional[ContainerCpuReading]:
    try:
        if scope.version == "v2":
            stat = _parse_stat(read_cgroup_file(scope.path / "cpu.stat"))
            usage_usec = stat.get("usage_usec")
            nr_throttled = stat.get("nr_throttled")
            throttled_usec = stat.get("throttled_usec")
        else:
            usage_dir = _sibling_controller_dir(scope.path, "cpu", "cpuacct")
            usage_ns = _parse_counter(read_cgroup_file(usage_dir / "cpuacct.usage")) if usage_dir else None
            usage_usec = usage_ns // 1000 if usage_ns is not None else None
            stat = _parse_stat(read_cgroup_file(scope.path / "cpu.stat"))
            nr_throttled = stat.get("nr_throttled")
            throttled_ns = stat.get("throttled_time")
            throttled_usec = throttled_ns // 1000 if throttled_ns is not None else None
        if usage_usec is None:
            return None
        return ContainerCpuReading(
            usage_usec=usage_usec,
            nr_throttled=nr_throttled,
            throttled_usec=throttled_usec,
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Legacy accessors, kept for the flat memory_limit / cpu_limit fields
# ---------------------------------------------------------------------------

def get_cgroup_memory_limit() -> Optional[int]:
    scope = select_memory_scope()
    return scope.limit if scope else None


def get_cgroup_memory_usage() -> Optional[int]:
    scope = select_memory_scope()
    if scope is None:
        return None
    reading = read_memory_usage(scope)
    return reading.current if reading else None


def get_cgroup_cpu_limit() -> Optional[float]:
    scope = select_cpu_scope()
    return scope.limit if scope else None


# ---------------------------------------------------------------------------
# Runtime and platform detection
# ---------------------------------------------------------------------------

def detect_container() -> Dict[str, Any]:
    """Detect whether this process runs in a container and which cgroup version it sees."""
    runtime = "bare-metal"
    is_containerized = False
    cgroup_version: Optional[str] = None

    if (CGROUP_ROOT / "cgroup.controllers").exists():
        cgroup_version = "v2"
    elif (CGROUP_ROOT / "cpu" / "cpu.cfs_quota_us").exists():
        cgroup_version = "v1"

    if DOCKERENV.exists():
        runtime = "docker"
        is_containerized = True
    else:
        cgroup_content = read_cgroup_file(PROC_1_CGROUP) or ""
        if "docker" in cgroup_content or "containerd" in cgroup_content:
            runtime = "docker"
            is_containerized = True
        elif "lxc" in cgroup_content:
            runtime = "lxc"
            is_containerized = True

    if os.environ.get("container") == "lxc":
        runtime = "lxc"
        is_containerized = True

    return {
        "is_containerized": is_containerized,
        "runtime": runtime,
        "cgroup_version": cgroup_version,
    }


def detect_virtualization() -> Tuple[Optional[bool], Optional[str]]:
    """(is_virtual_machine, platform_vendor); None for either when it cannot be read."""
    is_vm: Optional[bool] = None
    cpuinfo = read_cgroup_file(PROC_CPUINFO)
    if cpuinfo is not None:
        for line in cpuinfo.splitlines():
            if not line.startswith("flags"):
                continue
            _, _, flags = line.partition(":")
            is_vm = "hypervisor" in flags.split()
            break
    vendor = read_cgroup_file(DMI_SYS_VENDOR)
    return is_vm, vendor or None
