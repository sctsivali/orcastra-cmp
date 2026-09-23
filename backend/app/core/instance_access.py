"""Authorize one instance in one LXD project, and name the project a request acts on.

A Partner is scoped by cluster, and on a cluster bound to several organizations only the
project bound (org_scope.project_is_visible) keeps one organization out of the other's
projects. A Tenant is held by the ACL. Routes that took a project from the caller checked the
cluster alone, and a route that took no project let the service search every project and act
on the first same-named instance it found, which on a shared cluster can belong to someone
else.

Every route that reads or acts on one instance authorizes here first, before any try block,
and then passes the concrete project it authorized to the service, never the raw query value.
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Optional

from fastapi import HTTPException, status

from app.core.org_scope import (
    check_tenant_permission,
    project_is_visible,
    verify_cluster_access,
    verify_tenant_permission,
    visible_projects_for,
)
from app.core.tenant_visibility import parse_lxd_reference
from app.services.lxd.availability import unreachable_detail
from app.services.lxd.exceptions import LXDConnectionError
from app.services.lxd.utils import find_instance_in_projects

if TYPE_CHECKING:
    from app.services.lxd.lxd_service import LXDService

logger = logging.getLogger(__name__)

DEFAULT_PROJECT = "default"


def normalize_project(project: Optional[str]) -> str:
    """The LXD project a request means: a blank or missing value is the default project."""
    return (project or "").strip() or DEFAULT_PROJECT


def verify_project_access(cluster_id: str, project: Optional[str]) -> str:
    """Raise 403 unless this request may reach `project` on `cluster_id`, and return its name.

    important: the project bound is not behind the tenant permission flag. It is the only
    isolation a Partner has on a shared cluster, so it holds whatever the flag says.
    """
    name = normalize_project(project)
    verify_cluster_access(cluster_id)
    if not project_is_visible(cluster_id, name):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to this project",
        )
    return name


def verify_whole_cluster_access(cluster_id: str) -> None:
    """Raise 403 unless this request may change state that belongs to the whole cluster.

    important: a caller held to a project bound on `cluster_id` shares it with another
    organization. LXD auth groups, identities and server-level entitlements belong to no project,
    so a server `admin` key, a group with any permission list, or an identity added to or deleted
    from a group would reach the other organization's projects. On such a cluster only a caller
    with no bound (an admin) may write them.
    """
    verify_cluster_access(cluster_id)
    if visible_projects_for(cluster_id) is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not permitted on a cluster shared with another organization",
        )


def can_view_instance(cluster_id: str, project: Optional[str], instance_name: str) -> bool:
    """Whether this request may see `instance_name` in `project`: the bound and the ACL."""
    name = normalize_project(project)
    return project_is_visible(cluster_id, name) and check_tenant_permission(
        cluster_id, name, instance_name, "view"
    )


def verify_instance_view_access(
    cluster_id: str, project: Optional[str], instance_name: str
) -> str:
    """Raise 403 unless this request may view the instance, and return the project name.

    The tenant `view` check is unconditional here: a read or an action on one instance must
    never reach an instance its caller cannot see in the list.
    """
    name = verify_project_access(cluster_id, project)
    verify_tenant_permission(cluster_id, name, instance_name, "view")
    return name


def verify_tag_target(
    resource_type: str,
    resource_id: str,
    host: Optional[str],
    project: Optional[str],
    *,
    read: bool,
) -> None:
    """Hold a tag read or write to the resource it names, not only to the cluster in the query.

    important: a tag write is a real LXD write (user.tags on the node, project or instance) and a
    row keyed by host and project, so a Partner on a shared cluster could tag another
    organization's instance or project. The node a node tag names is the cluster itself, which
    the query `host` never covered. The project is normalized for the check only: the tag store
    keys rows on the raw value, so callers keep passing that down unchanged.
    """
    if resource_type == "node":
        verify_cluster_access(resource_id)
    elif host and resource_type == "project":
        name = verify_project_access(host, resource_id)
        if read:
            verify_tenant_permission(host, name, None, "view")
    elif host and resource_type == "instance":
        if read:
            verify_instance_view_access(host, project, resource_id)
        else:
            verify_project_access(host, project)


def _first_viewable_project(
    lxd_service: "LXDService", cluster_id: str, instance_name: str
) -> Optional[str]:
    """The first project this request may view that holds the instance. Blocking, so threaded."""
    client = lxd_service.get_client_with_reconnect(cluster_id)
    if client is None:
        raise LXDConnectionError(unreachable_detail(cluster_id))
    try:
        names = [p.name for p in client.projects.all()]
    except Exception as exc:  # noqa: BLE001 - a daemon without projects answers for default
        logger.warning(f"Could not list projects on {cluster_id}, using default: {exc}")
        names = [DEFAULT_PROJECT]
    for name in names:
        if not can_view_instance(cluster_id, name, instance_name):
            continue
        if find_instance_in_projects(client, instance_name, name)[0] is not None:
            return name
    return None


async def resolve_instance_project(
    lxd_service: "LXDService",
    cluster_id: str,
    instance_name: str,
    project: Optional[str],
) -> str:
    """Authorize and return the one project an instance request acts on.

    A named project is authorized as it is and returned, with no search. An omitted project
    is looked for only among the projects this request may view, so the answer can never be
    another organization's same-named instance, and nothing found is a 404.

    important: the search runs in a worker that carries the request context (asyncio.to_thread),
    because can_view_instance reads the org-scope ContextVars and an unscoped search would pass.
    """
    if (project or "").strip():
        return verify_instance_view_access(cluster_id, project, instance_name)
    verify_cluster_access(cluster_id)
    try:
        found = await asyncio.to_thread(
            _first_viewable_project, lxd_service, cluster_id, instance_name
        )
    except LXDConnectionError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    if found is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Instance {instance_name} not found on {cluster_id}",
        )
    return found


def operation_projects(operation: dict[str, Any]) -> set[str]:
    """Every LXD project an operation's resources name, whatever kind of resource they are.

    note: a reference with no `?project=` is the default project for a project resource and
    names no project at all for a cluster-level one such as a storage pool. Both read as
    default, which every bound holds, so neither kind widens or narrows what a caller sees.
    """
    resources = operation.get("resources") or {}
    projects: set[str] = set()
    for references in resources.values() if isinstance(resources, dict) else []:
        for reference in references if isinstance(references, list) else []:
            if isinstance(reference, str):
                projects.add(normalize_project(parse_lxd_reference(reference)[0]))
    return projects


def operation_targets(operation: dict[str, Any]) -> set[tuple[str, str]]:
    """The (project, instance) pairs an LXD operation names in its resources.

    LXD writes each as `/1.0/instances/<name>?project=<project>`, with no query for default.
    Matching on the pair, not on a substring of the path, is what keeps one organization's
    console session out of another's kill or active-session read on a shared cluster.
    """
    resources = operation.get("resources") or {}
    targets: set[tuple[str, str]] = set()
    for reference in resources.get("instances") or []:
        project, instance = parse_lxd_reference(reference)
        if instance:
            targets.add((normalize_project(project), instance))
    return targets
