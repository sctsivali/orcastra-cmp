"""Read the few series the instance dialog needs out of LXD's /1.0/metrics (OpenMetrics text).

LXD's state endpoint reports VM memory as MemTotal - MemFree, which counts page cache as used,
and has no disk I/O counters at all. /1.0/metrics carries both, per instance.

Only the families below are parsed; everything else in the scrape (Go runtime, API counters,
per-CPU seconds) is skipped before its labels are read. Both `lxd_` and `incus_` prefixes are
accepted. A malformed line is skipped and counted, never fatal: one odd line from a newer
daemon must not cost the whole sample.
"""

import logging
import math
import re
from dataclasses import dataclass, field, replace
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_LINE = re.compile(r"^(?P<name>[A-Za-z_:][A-Za-z0-9_:]*)(?:\{(?P<labels>.*)\})?\s+(?P<value>\S+)(?:\s+\S+)?\s*$")
# important: anchored on the start or a comma. Unanchored, a long run of name characters with
# no `="` after it made findall rescan the run from every offset: quadratic, and 40 KB of text
# from a hostile endpoint held a worker's GIL for over ten seconds.
_LABEL = re.compile(r'(?:^|,)\s*([A-Za-z_][A-Za-z0-9_]*)="((?:[^"\\]|\\.)*)"')
# A real LXD line is under 300 characters; anything far longer is not one worth parsing.
_MAX_LINE = 4096
_PREFIXES = ("lxd_", "incus_")

# note: whole disks only. A partition repeats its disk's bytes (sda1 is part of sda), a loop
# device inside a guest is a snap mount, and dm-*, md* and zd* sit on top of a disk that is
# also listed, so counting any of them adds the same bytes twice.
_WHOLE_DISK = re.compile(r"^(?:sd[a-z]+|vd[a-z]+|xvd[a-z]+|hd[a-z]+|nvme\d+n\d+|mmcblk\d+)$")

_FAMILIES = {
    "cpu_effective_total": "cpu_effective",
    "memory_MemTotal_bytes": "mem_total",
    "memory_MemAvailable_bytes": "mem_available",
    "memory_MemFree_bytes": "mem_free",
    "memory_Cached_bytes": "mem_cached",
    "memory_Shmem_bytes": "mem_shmem",
    "disk_read_bytes_total": "disk_read",
    "disk_written_bytes_total": "disk_written",
}


@dataclass(frozen=True)
class InstanceSeries:
    instance_type: Optional[str] = None
    cpu_effective: Optional[float] = None
    mem_total: Optional[float] = None
    mem_available: Optional[float] = None
    mem_free: Optional[float] = None
    mem_cached: Optional[float] = None
    mem_shmem: Optional[float] = None
    disk_read: Optional[float] = None
    disk_written: Optional[float] = None
    disk_devices: tuple = field(default_factory=tuple)

    def to_json(self) -> Dict[str, object]:
        return {
            "type": self.instance_type,
            "cpu_effective": self.cpu_effective,
            "mem_total": self.mem_total,
            "mem_available": self.mem_available,
            "mem_free": self.mem_free,
            "mem_cached": self.mem_cached,
            "mem_shmem": self.mem_shmem,
            "disk_read": self.disk_read,
            "disk_written": self.disk_written,
            "disk_devices": list(self.disk_devices),
        }

    @classmethod
    def from_json(cls, data: Dict[str, object]) -> "InstanceSeries":
        def number(key: str) -> Optional[float]:
            value = data.get(key)
            return float(value) if isinstance(value, (int, float)) else None

        kind = data.get("type")
        return cls(
            instance_type=kind if isinstance(kind, str) else None,
            cpu_effective=number("cpu_effective"),
            mem_total=number("mem_total"),
            mem_available=number("mem_available"),
            mem_free=number("mem_free"),
            mem_cached=number("mem_cached"),
            mem_shmem=number("mem_shmem"),
            disk_read=number("disk_read"),
            disk_written=number("disk_written"),
            disk_devices=tuple(str(d) for d in data.get("disk_devices") or ()),
        )


def _unescape(value: str) -> str:
    return value.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")


def parse_instance_series(text: str, *, project: str) -> Dict[str, InstanceSeries]:
    """Per-instance series of one project, keyed by instance name."""
    found: Dict[str, Dict[str, object]] = {}
    malformed = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if len(line) > _MAX_LINE:
            malformed += 1
            continue
        prefix = next((p for p in _PREFIXES if line.startswith(p)), None)
        if prefix is None:
            continue
        brace = line.find("{")
        space = line.find(" ")
        end = brace if brace != -1 and (space == -1 or brace < space) else space
        family = line[len(prefix) : end] if end > 0 else ""
        target = _FAMILIES.get(family)
        if target is None:
            continue
        match = _LINE.match(line)
        if match is None:
            malformed += 1
            continue
        try:
            value = float(match.group("value"))
        except ValueError:
            malformed += 1
            continue
        if not math.isfinite(value):
            continue
        labels = {key: _unescape(val) for key, val in _LABEL.findall(match.group("labels") or "")}
        name = labels.get("name")
        if not name or labels.get("project", project) != project:
            continue
        entry = found.setdefault(name, {"type": labels.get("type"), "disk_devices": set()})
        if target in ("disk_read", "disk_written"):
            device = labels.get("device", "")
            if not _WHOLE_DISK.match(device):
                continue
            entry["disk_devices"].add(device)  # type: ignore[union-attr]
            entry[target] = float(entry.get(target) or 0.0) + value  # type: ignore[arg-type]
        else:
            entry[target] = value
    if malformed:
        logger.debug("Skipped %d malformed metrics line(s) for project %s", malformed, project)
    series: Dict[str, InstanceSeries] = {}
    for name, values in found.items():
        devices = tuple(sorted(values.pop("disk_devices")))  # type: ignore[arg-type]
        kind = values.pop("type")
        series[name] = replace(
            InstanceSeries(instance_type=kind if isinstance(kind, str) else None),
            disk_devices=devices,
            **{key: float(val) for key, val in values.items()},  # type: ignore[arg-type]
        )
    return series


def honest_memory(series: InstanceSeries) -> Optional[Dict[str, Optional[int]]]:
    """Used memory without the page cache the guest can reclaim.

    A virtual machine's figures are its own kernel's meminfo, read by lxd-agent, so
    `MemTotal - MemAvailable` is what `free` inside it calls in use.

    important: a container's are derived by LXD from its cgroup, and LXD 5.21 sets
    MemAvailable = MemFree + Cached, where Cached is every file page, shared memory and tmpfs
    included. Those cannot be reclaimed, so a container holding 200 MiB in /dev/shm read as
    2 MiB used. Only Cached - Shmem is reclaimable: used = (MemTotal - MemFree) - (Cached - Shmem).
    """
    total, available, free = series.mem_total, series.mem_available, series.mem_free
    cached, shmem = series.mem_cached, series.mem_shmem or 0.0
    if not total or total <= 0:
        return None
    reclaimable = max(0.0, cached - shmem) if cached is not None else None
    if series.instance_type == "container":
        if free is None or reclaimable is None:
            return None
        used = (total - free) - reclaimable
    else:
        if available is None or available < 0:
            return None
        used = total - available
    used = max(0.0, min(used, total))
    return {
        "used": int(used),
        "total": int(total),
        "available": int(total - used),
        "cache": int(reclaimable) if reclaimable is not None else None,
    }
