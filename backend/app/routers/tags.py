"""
Tag Management API

Provides comprehensive REST API for tag operations:
- Tag CRUD (Create, Read, Update, Delete)
- Tag categories management
- Bulk tag operations
- Resource tag assignments (nodes, projects, instances)
- User visibility settings
- Statistics and analytics
- LXD sync

API Prefix: /api/v1/tags
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import List, Optional

from app.core.auth import get_current_user
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import (
    AuthenticatedUser,
    check_read_only_mode,
    require_admin,
    require_partner,
    require_tenant,
)
from app.core.org_scope import (
    org_scope_dependency,
    verify_cluster_access,
    verify_instance_view_access,
    verify_project_access,
    verify_tenant_permission,
)
from app.models.schemas import (
    # Tag schemas
    TagCreate, TagUpdate, TagResponse,
    TagCategoryCreate, TagCategoryUpdate, TagCategoryResponse,
    TagListResponse, TagStatsResponse,
    # Assignment schemas
    BulkTagAssignRequest, BulkTagAssignResponse,
    ResourceTagsResponse, TagGrouped,
    # User visibility
    UserTagVisibilitySettings, UserTagVisibilityResponse,
)
from app.models.db_models import TagAssignment
from app.services.lxd_service import get_lxd_service
from app.services.migration_fence import (
    MigrationFenceConflict,
    assert_cluster_not_migration_fenced,
    assert_instance_not_migration_fenced,
    assert_project_not_migration_fenced,
    guard_migration_mutations,
)
from app.services.tag_service import TagService

logger = logging.getLogger(__name__)

_RESOURCE_TYPES = {"node", "project", "instance"}


def _validate_resource_type(resource_type: str) -> None:
    if resource_type not in _RESOURCE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="resource_type must be 'node', 'project', or 'instance'",
        )


def _required_host(resource_type: str, host: Optional[str]) -> str:
    if not (host or "").strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"host is required for {resource_type} tag operations",
        )
    return host.strip()


def _target_cluster_id(
    resource_type: str,
    resource_id: str,
    host: Optional[str],
) -> str:
    return resource_id if resource_type == "node" else _required_host(resource_type, host)


def _authorize_resource_target(
    resource_type: str,
    resource_id: str,
    *,
    host: Optional[str],
    project: Optional[str],
) -> tuple[Optional[str], Optional[str], str]:
    """Authorize one tag target and return the exact values the service must use.

    `host` and `project` may originate from query parameters or a bulk request body. Resolving
    them before this helper and carrying these returned values into TagService prevents a body
    target from bypassing a query-only check. Project tags use `resource_id` as their LXD
    project; instance tags use `project` and always include it in the database lookup, so two
    same-named instances in different projects cannot share assignments.
    """
    if resource_type == "node":
        # Node tags use resource_id as the cluster connection key in TagService. Authorizing an
        # optional, unrelated `host` would check one cluster and mutate another.
        return None, None, resource_id

    cluster_id = _required_host(resource_type, host)
    if resource_type == "project":
        scoped_project = verify_project_access(cluster_id, resource_id)
        verify_tenant_permission(cluster_id, scoped_project, None, "view")
        return cluster_id, None, scoped_project

    scoped_project = verify_instance_view_access(cluster_id, project, resource_id)
    return cluster_id, scoped_project, resource_id


def _tag_migration_targets(
    resource_type: str,
    resource_id: str,
    *,
    host: Optional[str],
    project: Optional[str],
) -> tuple[set[str], set[tuple[str, str]], set[tuple[str, str, str]]]:
    clusters: set[str] = set()
    projects: set[tuple[str, str]] = set()
    instances: set[tuple[str, str, str]] = set()
    if resource_type == "instance" and host and project:
        instances.add((host, project, resource_id))
    elif resource_type == "project" and host:
        projects.add((host, resource_id))
    elif resource_type == "node":
        clusters.add(resource_id)
    else:
        raise MigrationFenceConflict()
    return clusters, projects, instances


async def _assert_tag_target_not_migration_fenced(
    resource_type: str,
    resource_id: str,
    *,
    host: Optional[str],
    project: Optional[str],
) -> None:
    """Compatibility check for callers that only need a side-effect-free admission probe."""
    try:
        if resource_type == "instance":
            await assert_instance_not_migration_fenced(
                cluster_id=str(host),
                project=str(project),
                instance_name=resource_id,
            )
        elif resource_type == "project":
            await assert_project_not_migration_fenced(
                cluster_id=str(host),
                project=resource_id,
            )
        else:
            await assert_cluster_not_migration_fenced(cluster_id=resource_id)
    except MigrationFenceConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc


@asynccontextmanager
async def _guard_tag_target_mutation(
    resource_type: str,
    resource_id: str,
    *,
    host: Optional[str],
    project: Optional[str],
) -> AsyncIterator[None]:
    """Keep Orcastra assignments and legacy LXD tags stable under one cluster lock."""
    try:
        clusters, projects, instances = _tag_migration_targets(
            resource_type,
            resource_id,
            host=host,
            project=project,
        )
        async with guard_migration_mutations(
            clusters=clusters,
            projects=projects,
            instances=instances,
        ):
            yield
    except MigrationFenceConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc


@asynccontextmanager
async def _guard_tag_definition_mutation(
    tag_id: int,
    tag_service: TagService,
) -> AsyncIterator[None]:
    """Protect every resource whose durable assignment references one tag definition."""
    rows = (
        await tag_service.db.execute(
            select(TagAssignment).where(TagAssignment.tag_id == tag_id)
        )
    ).scalars().all()
    targets = {
        (
            assignment.resource_type,
            assignment.resource_id,
            assignment.resource_host,
            assignment.resource_project,
        )
        for assignment in rows
    }
    if not targets:
        yield
        return

    clusters: set[str] = set()
    projects: set[tuple[str, str]] = set()
    instances: set[tuple[str, str, str]] = set()
    try:
        for resource_type, resource_id, host, project in targets:
            target_clusters, target_projects, target_instances = _tag_migration_targets(
                resource_type,
                resource_id,
                host=host,
                project=project,
            )
            clusters.update(target_clusters)
            projects.update(target_projects)
            instances.update(target_instances)
        async with guard_migration_mutations(
            clusters=clusters,
            projects=projects,
            instances=instances,
        ):
            current_rows = (
                await tag_service.db.execute(
                    select(TagAssignment).where(TagAssignment.tag_id == tag_id)
                )
            ).scalars().all()
            current_targets = {
                (
                    assignment.resource_type,
                    assignment.resource_id,
                    assignment.resource_host,
                    assignment.resource_project,
                )
                for assignment in current_rows
            }
            if current_targets != targets:
                raise MigrationFenceConflict()
            yield
    except MigrationFenceConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc


router = APIRouter(
    prefix="/tags",
    tags=["Tags"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)


# ============================================
# Dependency Injection
# ============================================

async def get_tag_service_dep(
    db: AsyncSession = Depends(get_db),
) -> TagService:
    """Get TagService with LXD service for sync."""
    try:
        lxd_service = get_lxd_service()
    except Exception:
        lxd_service = None
    return TagService(db=db, lxd_service=lxd_service)


def get_user_id(
    user: Optional[AuthenticatedUser] = Depends(get_current_user),
) -> Optional[str]:
    """The verified caller's email, which keys created_by, assigned_by and tag visibility.

    important: not a request header. This read X-User-ID, which any caller can set, so a request
    could stamp another user as a tag's author and read or overwrite their visibility settings.
    No client sent the header.
    """
    return getattr(user, "email", None)


# ============================================
# Tag Category Endpoints
# ============================================

@router.get(
    "/categories",
    response_model=List[TagCategoryResponse],
    summary="List all tag categories",
    description="Get all tag categories with their tag counts.",
)
async def list_categories(
    include_inactive: bool = Query(False, description="Include inactive categories"),
    tag_service: TagService = Depends(get_tag_service_dep),
):
    """List all tag categories."""
    return await tag_service.list_categories(include_inactive=include_inactive)


@router.post(
    "/categories",
    response_model=TagCategoryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new tag category",
    description="Create a new tag category with a unique prefix.",
)
async def create_category(
    data: TagCategoryCreate,
    tag_service: TagService = Depends(get_tag_service_dep),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Create a new tag category."""
    try:
        return await tag_service.create_category(data)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.patch(
    "/categories/{category_id}",
    response_model=TagCategoryResponse,
    summary="Update a tag category",
)
async def update_category(
    category_id: int,
    data: TagCategoryUpdate,
    tag_service: TagService = Depends(get_tag_service_dep),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Update a tag category."""
    try:
        return await tag_service.update_category(category_id, data)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


# ============================================
# Tag CRUD Endpoints
# ============================================

@router.get(
    "",
    response_model=TagListResponse,
    summary="List all tags",
    description="Get all tags with optional filtering by category or search term.",
)
async def list_tags(
    category_id: Optional[int] = Query(None, description="Filter by category ID"),
    search: Optional[str] = Query(None, description="Search in tag name"),
    user_id: Optional[str] = Depends(get_user_id),
    tag_service: TagService = Depends(get_tag_service_dep),
):
    """List all tags."""
    return await tag_service.list_tags(
        category_id=category_id,
        search=search,
        user_id=user_id,
    )


@router.post(
    "",
    response_model=TagResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new tag",
    description="Create a new tag. Category is auto-detected from prefix if not specified.",
)
async def create_tag(
    data: TagCreate,
    user_id: Optional[str] = Depends(get_user_id),
    tag_service: TagService = Depends(get_tag_service_dep),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Create a new tag."""
    try:
        return await tag_service.create_tag(data, user_id=user_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.patch(
    "/{tag_id}",
    response_model=TagResponse,
    summary="Update a tag",
)
async def update_tag(
    tag_id: int,
    data: TagUpdate,
    tag_service: TagService = Depends(get_tag_service_dep),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Update a tag."""
    try:
        async with _guard_tag_definition_mutation(tag_id, tag_service):
            return await tag_service.update_tag(tag_id, data)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@router.delete(
    "/{tag_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a tag",
    description="Delete a tag and all its assignments. System tags cannot be deleted.",
)
async def delete_tag(
    tag_id: int,
    tag_service: TagService = Depends(get_tag_service_dep),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Delete a tag."""
    try:
        async with _guard_tag_definition_mutation(tag_id, tag_service):
            await tag_service.delete_tag(tag_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


# ============================================
# Tag Statistics Endpoints
# ============================================

@router.get(
    "/stats",
    response_model=TagStatsResponse,
    summary="Get tag statistics",
    description="Get comprehensive tag usage statistics.",
)
async def get_statistics(
    tag_service: TagService = Depends(get_tag_service_dep),
):
    """Get tag statistics."""
    return await tag_service.get_tag_statistics()


# ============================================
# Resource Tag Assignment Endpoints
# ============================================

@router.get(
    "/resources/{resource_type}/{resource_id}",
    response_model=ResourceTagsResponse,
    summary="Get tags for a resource",
    description="Get all tags assigned to a resource with grouped view.",
)
async def get_resource_tags(
    resource_type: str,
    resource_id: str,
    host: Optional[str] = Query(None, description="LXD host (for project/instance)"),
    project: Optional[str] = Query(None, description="LXD project (for instance)"),
    tag_service: TagService = Depends(get_tag_service_dep),
):
    """Get tags for a resource."""
    _validate_resource_type(resource_type)
    verify_cluster_access(_target_cluster_id(resource_type, resource_id, host))
    if resource_type == "instance":
        # Keep the tenant-instance boundary visible in this tenant-reachable handler. The
        # shared target resolver below also uses this helper, but an indirect-only gate is easy
        # to remove accidentally while refactoring another resource type.
        host = _required_host(resource_type, host)
        project = verify_instance_view_access(host, project, resource_id)
    host, project, resource_id = _authorize_resource_target(
        resource_type,
        resource_id,
        host=host,
        project=project,
    )
    
    return await tag_service.get_resource_tags(
        resource_type=resource_type,
        resource_id=resource_id,
        resource_host=host,
        resource_project=project,
    )


@router.put(
    "/resources/{resource_type}/{resource_id}",
    response_model=BulkTagAssignResponse,
    summary="Set tags for a resource",
    description="Replace all tags for a resource with the provided list.",
)
async def set_resource_tags(
    resource_type: str,
    resource_id: str,
    request: BulkTagAssignRequest,
    host: Optional[str] = Query(None, description="LXD host (for project/instance)"),
    project: Optional[str] = Query(None, description="LXD project (for instance)"),
    user_id: Optional[str] = Depends(get_user_id),
    tag_service: TagService = Depends(get_tag_service_dep),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Set (replace) all tags for a resource."""
    effective_host = host if host is not None else request.resource_host
    effective_project = project if project is not None else request.resource_project
    _validate_resource_type(resource_type)
    verify_cluster_access(
        _target_cluster_id(resource_type, resource_id, effective_host)
    )
    effective_host, effective_project, resource_id = _authorize_resource_target(
        resource_type,
        resource_id,
        host=effective_host,
        project=effective_project,
    )
    async with _guard_tag_target_mutation(
        resource_type,
        resource_id,
        host=effective_host,
        project=effective_project,
    ):
        return await tag_service.set_resource_tags(
            resource_type=resource_type,
            resource_id=resource_id,
            tags=request.tags,
            resource_host=effective_host,
            resource_project=effective_project,
            user_id=user_id,
        )


@router.post(
    "/resources/{resource_type}/{resource_id}/add",
    response_model=BulkTagAssignResponse,
    summary="Add tags to a resource",
    description="Add tags to a resource without removing existing ones.",
)
async def add_resource_tags(
    resource_type: str,
    resource_id: str,
    request: BulkTagAssignRequest,
    host: Optional[str] = Query(None, description="LXD host (for project/instance)"),
    project: Optional[str] = Query(None, description="LXD project (for instance)"),
    user_id: Optional[str] = Depends(get_user_id),
    tag_service: TagService = Depends(get_tag_service_dep),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Add tags to a resource."""
    effective_host = host if host is not None else request.resource_host
    effective_project = project if project is not None else request.resource_project
    _validate_resource_type(resource_type)
    verify_cluster_access(
        _target_cluster_id(resource_type, resource_id, effective_host)
    )
    effective_host, effective_project, resource_id = _authorize_resource_target(
        resource_type,
        resource_id,
        host=effective_host,
        project=effective_project,
    )
    async with _guard_tag_target_mutation(
        resource_type,
        resource_id,
        host=effective_host,
        project=effective_project,
    ):
        return await tag_service.assign_tags_to_resource(
            BulkTagAssignRequest(
                tags=request.tags,
                resource_type=resource_type,
                resource_id=resource_id,
                resource_host=effective_host,
                resource_project=effective_project,
            ),
            user_id=user_id,
        )


@router.delete(
    "/resources/{resource_type}/{resource_id}/{tag_name}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a tag from a resource",
)
async def remove_resource_tag(
    resource_type: str,
    resource_id: str,
    tag_name: str,
    host: Optional[str] = Query(None, description="LXD host (for project/instance)"),
    project: Optional[str] = Query(None, description="LXD project (for instance)"),
    tag_service: TagService = Depends(get_tag_service_dep),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Remove a single tag from a resource."""
    _validate_resource_type(resource_type)
    verify_cluster_access(_target_cluster_id(resource_type, resource_id, host))
    host, project, resource_id = _authorize_resource_target(
        resource_type,
        resource_id,
        host=host,
        project=project,
    )
    async with _guard_tag_target_mutation(
        resource_type,
        resource_id,
        host=host,
        project=project,
    ):
        await tag_service.remove_tag_from_resource(
            tag_name=tag_name,
            resource_type=resource_type,
            resource_id=resource_id,
            resource_host=host,
            resource_project=project,
        )


# ============================================
# Legacy Endpoints (Backward Compatibility)
# ============================================

@router.put(
    "/instances/{host}/{instance_name}",
    response_model=BulkTagAssignResponse,
    summary="Update instance tags (legacy)",
    description="Legacy endpoint for updating instance tags.",
)
async def update_instance_tags_legacy(
    host: str,
    instance_name: str,
    request: BulkTagAssignRequest,
    project: str = Query("default", description="LXD project"),
    user_id: Optional[str] = Depends(get_user_id),
    tag_service: TagService = Depends(get_tag_service_dep),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Update tags for an instance (legacy endpoint)."""
    project = verify_instance_view_access(host, project, instance_name)
    async with _guard_tag_target_mutation(
        "instance",
        instance_name,
        host=host,
        project=project,
    ):
        return await tag_service.set_resource_tags(
            resource_type="instance",
            resource_id=instance_name,
            tags=request.tags,
            resource_host=host,
            resource_project=project,
            user_id=user_id,
        )


@router.put(
    "/projects/{host}/{project_name}",
    response_model=BulkTagAssignResponse,
    summary="Update project tags (legacy)",
)
async def update_project_tags_legacy(
    host: str,
    project_name: str,
    request: BulkTagAssignRequest,
    user_id: Optional[str] = Depends(get_user_id),
    tag_service: TagService = Depends(get_tag_service_dep),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Update tags for a project (legacy endpoint)."""
    project_name = verify_project_access(host, project_name)
    async with _guard_tag_target_mutation(
        "project",
        project_name,
        host=host,
        project=None,
    ):
        return await tag_service.set_resource_tags(
            resource_type="project",
            resource_id=project_name,
            tags=request.tags,
            resource_host=host,
            user_id=user_id,
        )


@router.put(
    "/nodes/{node_name}",
    response_model=BulkTagAssignResponse,
    summary="Update node tags (legacy)",
)
async def update_node_tags_legacy(
    node_name: str,
    request: BulkTagAssignRequest,
    user_id: Optional[str] = Depends(get_user_id),
    tag_service: TagService = Depends(get_tag_service_dep),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Update tags for a node (legacy endpoint)."""
    verify_cluster_access(node_name)
    async with _guard_tag_target_mutation(
        "node",
        node_name,
        host=None,
        project=None,
    ):
        return await tag_service.set_resource_tags(
            resource_type="node",
            resource_id=node_name,
            tags=request.tags,
            user_id=user_id,
        )


@router.get(
    "/instances/{host}/{instance_name}",
    response_model=ResourceTagsResponse,
    summary="Get instance tags (legacy)",
)
async def get_instance_tags_legacy(
    host: str,
    instance_name: str,
    project: str = Query("default", description="LXD project"),
    tag_service: TagService = Depends(get_tag_service_dep),
):
    """Get tags for an instance (legacy endpoint)."""
    project = verify_instance_view_access(host, project, instance_name)
    return await tag_service.get_resource_tags(
        resource_type="instance",
        resource_id=instance_name,
        resource_host=host,
        resource_project=project,
    )


# ============================================
# User Visibility Settings Endpoints
# ============================================

@router.get(
    "/visibility",
    response_model=UserTagVisibilityResponse,
    summary="Get user tag visibility settings",
)
async def get_user_visibility(
    user_id: Optional[str] = Depends(get_user_id),
    tag_service: TagService = Depends(get_tag_service_dep),
):
    """Get current user's tag visibility settings."""
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User ID required"
        )
    
    return await tag_service.get_user_visibility_response(user_id)


@router.put(
    "/visibility",
    response_model=UserTagVisibilityResponse,
    summary="Update user tag visibility settings",
)
async def update_user_visibility(
    settings: UserTagVisibilitySettings,
    user_id: Optional[str] = Depends(get_user_id),
    tag_service: TagService = Depends(get_tag_service_dep),
    _: None = Depends(check_read_only_mode),
):
    """Update current user's tag visibility settings."""
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User ID required"
        )
    
    return await tag_service.update_user_visibility(user_id, settings)


# ============================================
# LXD Sync Endpoints
# ============================================

@router.post(
    "/sync/import/{resource_type}/{resource_id}",
    response_model=BulkTagAssignResponse,
    summary="Import tags from LXD",
    description="Import existing tags from LXD's user.tags into PostgreSQL.",
)
async def import_from_lxd(
    resource_type: str,
    resource_id: str,
    host: Optional[str] = Query(None, description="LXD host"),
    project: Optional[str] = Query(None, description="LXD project"),
    user_id: Optional[str] = Depends(get_user_id),
    tag_service: TagService = Depends(get_tag_service_dep),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Import tags from LXD."""
    _validate_resource_type(resource_type)
    verify_cluster_access(_target_cluster_id(resource_type, resource_id, host))
    host, project, resource_id = _authorize_resource_target(
        resource_type,
        resource_id,
        host=host,
        project=project,
    )
    async with _guard_tag_target_mutation(
        resource_type,
        resource_id,
        host=host,
        project=project,
    ):
        return await tag_service.import_tags_from_lxd(
            resource_type=resource_type,
            resource_id=resource_id,
            resource_host=host,
            resource_project=project,
            user_id=user_id,
        )


# ============================================
# Utility Endpoints
# ============================================

@router.post(
    "/classify",
    response_model=TagGrouped,
    summary="Classify tags by category",
    description="Group a list of tags by their category prefix.",
)
async def classify_tags(
    tags: List[str],
):
    """Classify tags into groups."""
    return TagService.group_tags(tags)


@router.post(
    "/validate",
    summary="Validate tag names",
)
async def validate_tags(
    tags: List[str],
):
    """Validate a list of tag names."""
    results = {}
    for tag in tags:
        is_valid, error = TagService.validate_tag_name(tag)
        results[tag] = {
            "valid": is_valid,
            "error": error if not is_valid else None,
        }
    return results


@router.post(
    "/init-categories",
    summary="Initialize default categories",
    description="Initialize default tag categories (admin only).",
)
async def init_default_categories(
    tag_service: TagService = Depends(get_tag_service_dep),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_admin()),
):
    """Initialize default tag categories."""
    count = await tag_service.initialize_default_categories()
    return {
        "success": True,
        "message": f"Initialized {count} default categories",
        "created_count": count,
    }
