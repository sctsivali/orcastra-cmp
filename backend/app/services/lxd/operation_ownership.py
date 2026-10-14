"""Which project an LXD operation belongs to, read off the resources it names.

An operation id is cluster-wide: LXD answers GET /1.0/operations/{id} whatever ?project= says
(observed on nuc2, LXD 5.21, 2026-10-14). So every surface that hands an operation to a caller
scoped to some projects decides ownership here, from the resource URLs, and treats an operation
whose owner it cannot determine as not found.
"""

from dataclasses import dataclass
from typing import Any, FrozenSet, Mapping, Optional
from urllib.parse import parse_qs, unquote, urlsplit

__all__ = [
    "INSTANCE_ROOTS",
    "MAX_RESOURCE_URL_LENGTH",
    "OperationOwnership",
    "PROJECT_RESOURCE_ROOTS",
    "operation_ownership",
    "operation_value",
    "resource_project",
    "visible_to",
]

# An operation is project-owned only when every resource URL names one of these API families.
# Cluster members, certificates, storage pools themselves, and any future/unknown family are
# deliberately absent. A query parameter on a cluster-wide URL must never turn it into a
# project-owned resource by assertion alone.
PROJECT_RESOURCE_ROOTS = {
    "instances": frozenset({"instances", "containers", "virtual-machines"}),
    "containers": frozenset({"instances", "containers"}),
    "virtual-machines": frozenset({"instances", "virtual-machines"}),
    "instance-snapshots": frozenset({"instances", "containers", "virtual-machines"}),
    # The spelling LXD 5.21 actually writes for a snapshot operation ("instances_snapshots",
    # observed on nuc2). Without it every snapshot create, restore or delete had no owner.
    "instances-snapshots": frozenset({"instances", "containers", "virtual-machines"}),
    "instance-backups": frozenset({"instances", "containers", "virtual-machines"}),
    "images": frozenset({"images"}),
    "profiles": frozenset({"profiles"}),
    "networks": frozenset({"networks"}),
    "network-acls": frozenset({"network-acls"}),
    "network-address-sets": frozenset({"network-address-sets"}),
    "network-zones": frozenset({"network-zones"}),
    "projects": frozenset({"projects"}),
}
INSTANCE_ROOTS = frozenset({"instances", "containers", "virtual-machines"})
MAX_RESOURCE_URL_LENGTH = 4096


@dataclass(frozen=True)
class OperationOwnership:
    project: str
    instance_name: str = ""


def operation_value(operation: Any, key: str, default: Any = None) -> Any:
    if isinstance(operation, Mapping):
        return operation.get(key, default)
    return getattr(operation, key, default)


def resource_project(kind: str, raw_url: Any) -> Optional[tuple[str, str]]:
    """Return a resource's project and optional instance, or None when ownership is unsafe."""
    if not isinstance(raw_url, str) or not raw_url or len(raw_url) > MAX_RESOURCE_URL_LENGTH:
        return None

    parsed = urlsplit(raw_url)
    if parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith("/1.0/"):
        return None

    segments = [unquote(segment) for segment in parsed.path.split("/") if segment]
    if len(segments) < 3 or segments[0] != "1.0":
        return None
    if any(segment in {".", ".."} or "/" in segment for segment in segments):
        return None

    normalized_kind = kind.replace("_", "-")
    root = segments[1]
    if normalized_kind in {"storage-volumes", "storage-volume-snapshots"}:
        if root != "storage-pools" or len(segments) < 6 or segments[3] != "volumes":
            return None
    elif normalized_kind == "storage-buckets":
        if root != "storage-pools" or len(segments) < 5 or segments[3] != "buckets":
            return None
    elif root not in PROJECT_RESOURCE_ROOTS.get(normalized_kind, frozenset()):
        return None

    try:
        query = parse_qs(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=False,
            max_num_fields=32,
        )
    except ValueError:
        return None
    project_values = query.get("project", [])
    if len(project_values) > 1 or (project_values and not project_values[0].strip()):
        return None

    if normalized_kind == "projects":
        project = segments[2].strip()
        if project_values and project_values[0] != project:
            return None
    else:
        project = project_values[0].strip() if project_values else "default"
    if not project:
        return None

    instance_name = segments[2] if root in INSTANCE_ROOTS else ""
    return project, instance_name


def operation_ownership(operation: Any) -> Optional[OperationOwnership]:
    """Derive one unambiguous project from all resources named by an operation."""
    resources = operation_value(operation, "resources")
    if not isinstance(resources, Mapping) or not resources:
        return None

    projects: set[str] = set()
    instances: set[str] = set()
    resource_count = 0
    for kind, urls in resources.items():
        if not isinstance(kind, str) or not isinstance(urls, list):
            return None
        for raw_url in urls:
            ownership = resource_project(kind, raw_url)
            if ownership is None:
                return None
            project, instance_name = ownership
            projects.add(project)
            if instance_name:
                instances.add(instance_name)
            resource_count += 1

    if resource_count == 0 or len(projects) != 1:
        return None
    return OperationOwnership(
        project=next(iter(projects)),
        instance_name=next(iter(instances)) if len(instances) == 1 else "",
    )


def visible_to(operation: Any, visible_projects: Optional[FrozenSet[str]]) -> bool:
    """Whether a caller holding `visible_projects` may see this operation; None means every project.

    An operation without a determinable owner is visible only to nobody scoped, never by default.
    """
    owner = operation_ownership(operation)
    return owner is not None and (visible_projects is None or owner.project in visible_projects)
