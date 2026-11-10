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


def references_in_projects(references: object, visible_projects: "frozenset[str] | set[str]") -> list:
    """The references in `references` whose project is one of `visible_projects`.

    For an API key's own grant, not for a tenant's policy (that is
    `filter_visible_lxd_references`). Anything that is not a string reference is dropped.
    """
    if not isinstance(references, list):
        return []
    return [
        reference for reference in references
        if isinstance(reference, str) and parse_lxd_reference(reference)[0] in visible_projects
    ]


def used_by_in_projects(answer: object, visible_projects: "frozenset[str] | set[str]") -> object:
    """Every `used_by` list anywhere in an LXD answer, kept to references in `visible_projects`.

    A network's, pool's, profile's or volume's used_by is cluster-wide, and an answer can hold one
    object or a recursion list of them, so the walk covers the whole envelope. Changes the answer in
    place and returns it.
    """
    stack = [answer]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "used_by":
                    node[key] = references_in_projects(value, visible_projects)
                elif isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(item for item in node if isinstance(item, (dict, list)))
    return answer


def tenant_can_view_lxd_reference(
    cluster_id: str,
    reference: str,
    required_permission: str = "view",
) -> bool:
    """Whether a tenant can see the resource an LXD reference points to.

    A reference that names no instance (a profile, network, ACL, custom volume or image) is never
    visible to a Tenant: `check_tenant_permission(..., None)` passes it on any rule in its
    project, which would expose another party's records in a shared project. A record a Tenant's
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

    The Partner project bound applies first: on a cluster shared by two organizations a record in
    `default` (a profile, a network, a pool) is used by both organizations' instances. A bare
    reference parses to `default`, which every bound contains. The tenant ACL applies second.
    """
    bounded = [
        reference for reference in (references or [])
        if project_is_visible(cluster_id, parse_lxd_reference(reference)[0])
    ]
    if not tenant_acl_active():
        return bounded
    # A Tenant does not see the cluster member a reference names (`&target=` on a
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

    The `used_by` filter alone does not cover lists past the default project: a reference like
    `/1.0/networks/foo` names no instance, and `check_tenant_permission` then merges permissions
    from any rule matching the project, so one instance-level rule in project P would expose every
    project-owned ACL in P. Ownership is therefore checked as well as usage. `default` stays
    visible: projects that do not isolate networks and profiles draw them from it, and the
    tenant's own instances are attached to them.
    """
    owner = project or "default"
    if owner == "default":
        return True
    return check_tenant_permission(cluster_id, owner, None, "view")
