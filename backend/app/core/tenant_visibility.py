"""Tenant visibility helpers for LXD resource references."""

from typing import Optional
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urlparse, urlsplit, urlunsplit

from app.core.org_scope import check_tenant_permission, get_tenant_acl, project_is_visible

_INSTANCE_VOLUME_TYPES = {"container", "virtual-machine"}


def tenant_acl_active() -> bool:
    """Return True when the current request is filtered by tenant ACL rules."""
    return get_tenant_acl() is not None


def tenant_acl_has_permission(
    acl: Optional[dict],
    cluster_id: str,
    project_name: str = "default",
    instance_name: Optional[str] = None,
    required_permission: str = "view",
) -> bool:
    """Check a captured tenant ACL without reading request contextvars."""
    if acl is None:
        return True

    cluster_rules = acl.get(cluster_id)
    if not cluster_rules:
        return False

    for (project_rule, instance_rule), permissions in cluster_rules.items():
        if required_permission not in permissions:
            continue
        if project_rule not in ("*", project_name):
            continue
        if instance_name is None or instance_rule in ("*", instance_name):
            return True
    return False


def without_member_target(reference: str) -> str:
    """A used_by reference without LXD's `target`, the cluster member it appends on a clustered pool."""
    parts = urlsplit(reference)
    if not parts.query:
        return reference
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "target"]
    return urlunsplit(parts._replace(query=urlencode(query)))


def parse_lxd_reference(reference: str) -> tuple[str, Optional[str]]:
    """Extract project and instance from an LXD used_by/resource reference."""
    if not reference:
        return "default", None

    parsed = urlparse(reference)
    query = parse_qs(parsed.query)
    project_values = query.get("project") or []
    project_name = unquote(project_values[0]) if project_values and project_values[0] else "default"
    parts = [unquote(part) for part in parsed.path.split("/") if part]

    if "projects" in parts:
        index = parts.index("projects")
        if len(parts) > index + 1:
            project_name = parts[index + 1]

    if "instances" in parts:
        index = parts.index("instances")
        if len(parts) > index + 1:
            return project_name, parts[index + 1]

    if "volumes" in parts:
        index = parts.index("volumes")
        if len(parts) > index + 2 and parts[index + 1] in _INSTANCE_VOLUME_TYPES:
            return project_name, parts[index + 2]

    return project_name, None


def tenant_can_view_lxd_reference(
    cluster_id: str,
    reference: str,
    required_permission: str = "view",
) -> bool:
    """Check if a tenant can see the resource pointed to by an LXD reference.

    important: a reference that names no instance (a profile, network, ACL, custom volume or
    image) is never visible to a Tenant. `check_tenant_permission(..., None)` passes it on any
    rule in its project, so one instance rule in a project shared with another party handed over
    that party's records (backlog A1-A7, owner decision 2026-11-05: strict). A record a Tenant's
    own instance uses still shows, through that instance's reference.
    """
    if not tenant_acl_active():
        return True
    project_name, instance_name = parse_lxd_reference(reference)
    if instance_name is None:
        return False
    return check_tenant_permission(cluster_id, project_name, instance_name, required_permission)


def tenant_acl_can_view_lxd_reference(
    acl: Optional[dict],
    cluster_id: str,
    reference: str,
    required_permission: str = "view",
) -> bool:
    """Check an LXD reference against a captured tenant ACL snapshot (same rule as above)."""
    project_name, instance_name = parse_lxd_reference(reference)
    if acl is not None and instance_name is None:
        return False
    return tenant_acl_has_permission(
        acl,
        cluster_id,
        project_name,
        instance_name,
        required_permission,
    )


def filter_visible_lxd_references(
    cluster_id: str,
    references: list[str],
    required_permission: str = "view",
) -> list[str]:
    """Keep only used_by/resource references the caller may see.

    The Partner project bound first: on a cluster shared by two organizations a record in the
    default project (a profile, a network, a pool) is used by both organizations' instances,
    and an unbounded used_by named the other one's. A bare reference parses to `default`,
    which every bound contains, so nothing an unbounded caller sees changes. Then the tenant
    ACL, as before.
    """
    bounded = [
        reference for reference in (references or [])
        if project_is_visible(cluster_id, parse_lxd_reference(reference)[0])
    ]
    if not tenant_acl_active():
        return bounded
    # note: a Tenant does not see the cluster member a reference names (`&target=` on a
    # clustered pool), as `vm_for_tier` withholds an instance's location.
    return [
        without_member_target(reference) for reference in bounded
        if tenant_can_view_lxd_reference(cluster_id, reference, required_permission)
    ]


def tenant_can_view_lxd_used_by(
    cluster_id: str,
    references: list[str],
    required_permission: str = "view",
) -> bool:
    """Return True when at least one used_by reference is tenant-visible."""
    return not tenant_acl_active() or bool(
        filter_visible_lxd_references(cluster_id, references, required_permission)
    )


def tenant_acl_can_view_lxd_used_by(
    acl: Optional[dict],
    cluster_id: str,
    references: list[str],
    required_permission: str = "view",
) -> bool:
    """Check used_by references against a captured tenant ACL snapshot."""
    return acl is None or any(
        tenant_acl_can_view_lxd_reference(acl, cluster_id, reference, required_permission)
        for reference in (references or [])
    )


def tenant_may_see_project(cluster_id: str, project: Optional[str]) -> bool:
    """Whether a tenant may see a record OWNED by this project.

    A security review pointed out that widening these lists past the default project changed
    what the `used_by` filter alone protects. `parse_lxd_reference` yields no instance name for
    a reference like `/1.0/networks/foo`, and `check_tenant_permission` then merges permissions
    from any rule matching the project, so a tenant holding one instance-level rule in project
    P could read every project-owned ACL in P, ingress and egress bodies included.

    So ownership is checked as well as usage, and since 2026-11-05 the usage side refuses a
    reference that names no instance (`tenant_can_view_lxd_reference`). `default` stays visible
    unconditionally: it is
    the shared project every other one draws its networks and profiles from when it does not
    isolate them, and hiding it would take away resources the tenant's own instances are
    attached to.
    """
    owner = project or "default"
    if owner == "default":
        return True
    return check_tenant_permission(cluster_id, owner, None, "view")
