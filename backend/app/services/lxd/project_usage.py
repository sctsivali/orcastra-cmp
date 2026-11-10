"""How much of each project limit is allocated, counted the way LXD counts it.

LXD's project limit checks (lxd/project/limits.go) sum, over every instance in the project
whatever its state, the expanded config (profiles applied) of `limits.cpu` and
`limits.memory`, and count instances by type against `limits.instances`,
`limits.containers` and `limits.virtual-machines`. The create-instance form shows the
result beside the operator's request, before the daemon refuses it. Aggregates only:
nothing here carries an instance name.
"""

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Set

from pylxd.exceptions import NotFound

from app.core.lxd_path import safe_segment

from .cpu_allocation import parse_cpu_limit
from .exceptions import LXDProjectNotFoundError
from .project_scope import ALL_PROJECTS_READ_TIMEOUT, list_in_project

logger = logging.getLogger(__name__)

_SIZE = re.compile(r"^(\d+(?:\.\d+)?)\s*([A-Za-z]*)$")
_UNITS = {
    "": 1, "b": 1,
    "kb": 10**3, "mb": 10**6, "gb": 10**9, "tb": 10**12, "pb": 10**15, "eb": 10**18,
    "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4, "pib": 1024**5, "eib": 1024**6,
}

LIMIT_KEYS = ("cpu", "memory_bytes", "instances", "containers", "virtual_machines", "disk_bytes")


def parse_lxd_size(value: Any) -> Optional[int]:
    """Bytes for an LXD size string ("8GiB", "512MB", "1024"); None for a percentage or junk."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    m = _SIZE.match(text)
    if not m:
        return None
    unit = _UNITS.get(m.group(2).lower())
    if unit is None:
        return None
    return int(float(m.group(1)) * unit)


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip()) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


@dataclass
class ProjectUsage:
    limits: Dict[str, Optional[int]]
    used: Dict[str, int]
    # False when the instance read failed: the limits are real, the usage is a floor of zero.
    complete: bool
    # Per-resource completeness, so a missing disk capability does not make a verified
    # CPU-only change look unknowable. ``None`` falls back to ``complete`` for every field,
    # for test doubles and callers that construct this type directly.
    complete_fields: Optional[Set[str]] = None

    def is_complete(self, field: str) -> bool:
        return self.complete if self.complete_fields is None else field in self.complete_fields


def _read_project_disk_allocation(client: Any, project: str) -> Optional[int]:
    """Read the daemon's aggregate disk allocation when the project_usage API exists.

    ``limits.disk`` includes instance volumes, custom volumes, and image volumes. Summing only
    instance root devices would undercount and could approve a growth the daemon must reject,
    so no local approximation is used.
    """
    # Read the cached handshake directly, as the rest of this service layer does: test
    # doubles and unusual daemons can omit the extension list, and PyLXD's
    # ``has_api_extension`` then raises KeyError.
    info = getattr(client, "host_info", None) or {}
    if "project_usage" not in (info.get("api_extensions") or []):
        return None
    response = client.api.projects[safe_segment(project)].state.get(
        timeout=ALL_PROJECTS_READ_TIMEOUT
    )
    metadata = response.json().get("metadata") or {}
    resources = metadata.get("resources") or {}
    resource = resources.get("limits.disk") or resources.get("disk")
    if not isinstance(resource, dict):
        return None
    if "Usage" in resource:
        raw_usage = resource["Usage"]
    elif "usage" in resource:
        raw_usage = resource["usage"]
    else:
        return None

    # Incus returns the project-state fields as ``Limit`` and ``Usage``; some test doubles
    # and daemon variants use lowercase keys. Booleans and floats are refused rather than
    # coerced into byte counts: an uncertain quota read must fail closed.
    if isinstance(raw_usage, bool) or not isinstance(raw_usage, (int, str)):
        return None
    try:
        usage = int(raw_usage.strip()) if isinstance(raw_usage, str) else raw_usage
    except ValueError:
        return None
    return usage if usage >= 0 else None


def _read_project_config(client: Any, project: str) -> Dict[str, str]:
    try:
        response = client.api.projects[safe_segment(project)].get(timeout=ALL_PROJECTS_READ_TIMEOUT)
    except NotFound:
        raise LXDProjectNotFoundError(project) from None
    return (response.json().get("metadata") or {}).get("config") or {}


def read_project_usage(client: Any, project: str) -> ProjectUsage:
    """One project read and one instance listing, summed by LXD's rule."""
    config = _read_project_config(client, project)
    limits = {
        "cpu": _int_or_none(config.get("limits.cpu")),
        "memory_bytes": parse_lxd_size(config.get("limits.memory")),
        "instances": _int_or_none(config.get("limits.instances")),
        "containers": _int_or_none(config.get("limits.containers")),
        "virtual_machines": _int_or_none(config.get("limits.virtual-machines")),
        "disk_bytes": parse_lxd_size(config.get("limits.disk")),
    }
    used = {
        "cpu": 0,
        "memory_bytes": 0,
        "instances": 0,
        "containers": 0,
        "virtual_machines": 0,
        "disk_bytes": 0,
    }
    complete = True
    instance_usage_complete = True
    complete_fields: Set[str] = set()
    try:
        rows = list_in_project(client, "instances", project, recursion=1)
    except Exception as e:
        # A limited project refuses creates it cannot account for, so a floor of zero has to
        # be labelled as one rather than shown as "0 of 4 used".
        logger.warning(f"Could not list instances of project {project} for usage: {e}")
        rows, complete, instance_usage_complete = [], False, False
    for row in rows:
        expanded = row.get("expanded_config") or row.get("config") or {}
        # default=0: in a project that limits CPU, LXD refuses an instance without
        # limits.cpu, so an unset value counts as zero here (the member read counts one core).
        used["cpu"] += parse_cpu_limit(expanded.get("limits.cpu"), default=0)
        used["memory_bytes"] += parse_lxd_size(expanded.get("limits.memory")) or 0
        used["instances"] += 1
        if row.get("type") == "virtual-machine":
            used["virtual_machines"] += 1
        else:
            used["containers"] += 1

    if instance_usage_complete:
        complete_fields.update(
            {"cpu", "memory_bytes", "instances", "containers", "virtual_machines"}
        )

    if limits["disk_bytes"] is None:
        complete_fields.add("disk_bytes")
    else:
        try:
            disk_usage = _read_project_disk_allocation(client, project)
        except Exception as exc:  # noqa: BLE001 - an unavailable aggregate must fail closed
            logger.warning("Could not read disk allocation of project %s: %s", project, exc)
            disk_usage = None
        if disk_usage is not None:
            used["disk_bytes"] = disk_usage
            complete_fields.add("disk_bytes")
        else:
            complete = False

    return ProjectUsage(
        limits=limits,
        used=used,
        complete=complete,
        complete_fields=complete_fields,
    )
