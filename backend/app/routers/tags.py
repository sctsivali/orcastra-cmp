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

from fastapi import APIRouter, HTTPException, status, Depends, Header, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional
import logging

from app.core.database import get_db
from app.core.dependencies import check_read_only_mode, require_tenant, require_partner, require_admin, AuthenticatedUser
from app.core.org_scope import org_scope_dependency
from app.services.tag_service import TagService
from app.services.lxd_service import get_lxd_service
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

logger = logging.getLogger(__name__)

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
    x_user_id: Optional[str] = Header(None, alias="X-User-ID"),
) -> Optional[str]:
    """Extract user ID from headers."""
    return x_user_id


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
    if resource_type not in ["node", "project", "instance"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="resource_type must be 'node', 'project', or 'instance'"
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
    if resource_type not in ["node", "project", "instance"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="resource_type must be 'node', 'project', or 'instance'"
        )
    
    return await tag_service.set_resource_tags(
        resource_type=resource_type,
        resource_id=resource_id,
        tags=request.tags,
        resource_host=host or request.resource_host,
        resource_project=project or request.resource_project,
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
    if resource_type not in ["node", "project", "instance"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="resource_type must be 'node', 'project', or 'instance'"
        )
    
    return await tag_service.assign_tags_to_resource(
        BulkTagAssignRequest(
            tags=request.tags,
            resource_type=resource_type,
            resource_id=resource_id,
            resource_host=host or request.resource_host,
            resource_project=project or request.resource_project,
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
    if resource_type not in ["node", "project", "instance"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="resource_type must be 'node', 'project', or 'instance'"
        )
    
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
    if resource_type not in ["node", "project", "instance"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="resource_type must be 'node', 'project', or 'instance'"
        )
    
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
