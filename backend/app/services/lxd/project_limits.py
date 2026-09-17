"""What LXD says when a project limit refuses a create, read into fields.

LXD's project limit checks (lxd/project/limits.go) refuse a create synchronously with one of
two sentences, which reach this service as the text of an LXDAPIException:

    Reached maximum aggregate value "4" for "limits.cpu" in project "web-tier"
    Reached maximum number of instances in project "web-tier"
    Reached maximum number of instances of type "virtual-machine" in project "web-tier"

A person reads those fine. A form cannot: it needs the limit key to mark a field, the project
to name, and the maximum to compare. This module is the one place that parses them.
"""

import re
from dataclasses import dataclass
from typing import Optional

_AGGREGATE = re.compile(
    r'Reached maximum aggregate value "(?P<max>[^"]+)" for "(?P<limit>limits\.[a-z-]+)" '
    r'in project "(?P<project>[^"]+)"'
)
_COUNT = re.compile(
    r'Reached maximum number of instances(?: of type "(?P<type>[^"]+)")? in project "(?P<project>[^"]+)"'
)

# LXD's instance-type limit keys, by the type name its message carries.
_COUNT_LIMIT_BY_TYPE = {
    "container": "limits.containers",
    "virtual-machine": "limits.virtual-machines",
}


@dataclass(frozen=True)
class ProjectLimitHit:
    """Which project limit refused the create."""

    limit: str
    project: str
    maximum: Optional[str] = None


def project_limit_from_lxd_error(text: str) -> Optional[ProjectLimitHit]:
    """The limit an LXD refusal names, or None when the text is about something else."""
    if not text:
        return None
    m = _AGGREGATE.search(text)
    if m:
        return ProjectLimitHit(limit=m.group("limit"), project=m.group("project"), maximum=m.group("max"))
    m = _COUNT.search(text)
    if m:
        limit = _COUNT_LIMIT_BY_TYPE.get(m.group("type") or "", "limits.instances")
        return ProjectLimitHit(limit=limit, project=m.group("project"))
    return None
