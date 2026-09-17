"""How much of each project limit is allocated, counted the way LXD counts it.

LXD's project limit checks (lxd/project/limits.go) sum, over every instance in the project
whatever its state, the expanded config (profiles applied) of `limits.cpu` and
`limits.memory`, and count instances by type against `limits.instances`,
`limits.containers` and `limits.virtual-machines`. The create-instance form shows the
result beside the operator's request, so the daemon's synchronous refusal is no longer the
first signal. Aggregates only: nothing here carries an instance name.
"""

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

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
    used = {"cpu": 0, "memory_bytes": 0, "instances": 0, "containers": 0, "virtual_machines": 0}
    complete = True
    try:
        rows = list_in_project(client, "instances", project, recursion=1)
    except Exception as e:
        # A limited project refuses creates it cannot account for, so a floor of zero has to
        # be labelled as one rather than shown as "0 of 4 used".
        logger.warning(f"Could not list instances of project {project} for usage: {e}")
        rows, complete = [], False
    for row in rows:
        expanded = row.get("expanded_config") or row.get("config") or {}
        # default=0: LXD refuses to create an instance without limits.cpu in a project that
        # limits it, so an unset value is nothing, not the member read's "one core".
        used["cpu"] += parse_cpu_limit(expanded.get("limits.cpu"), default=0)
        used["memory_bytes"] += parse_lxd_size(expanded.get("limits.memory")) or 0
        used["instances"] += 1
        if row.get("type") == "virtual-machine":
            used["virtual_machines"] += 1
        else:
            used["containers"] += 1
    return ProjectUsage(limits=limits, used=used, complete=complete)
