"""Tenant visibility helpers for LXD resource references."""

from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

from app.core.org_scope import check_tenant_permission, get_tenant_acl

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
    """Check if a tenant can see the resource pointed to by an LXD reference."""
    if not tenant_acl_active():
        return True
    project_name, instance_name = parse_lxd_reference(reference)
    return check_tenant_permission(cluster_id, project_name, instance_name, required_permission)


def tenant_acl_can_view_lxd_reference(
    acl: Optional[dict],
    cluster_id: str,
    reference: str,
    required_permission: str = "view",
) -> bool:
    """Check an LXD reference against a captured tenant ACL snapshot."""
    project_name, instance_name = parse_lxd_reference(reference)
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
    """Keep only used_by/resource references visible to the current tenant."""
    safe_references = references or []
    if not tenant_acl_active():
        return list(safe_references)
    return [
        reference for reference in safe_references
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