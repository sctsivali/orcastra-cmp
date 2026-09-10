"""
Tag Service

- PostgreSQL as source of truth
- Redis caching for performance
- LXD sync for backward compatibility
- Per-user visibility settings
- Category-based classification
"""

import logging
import re
from typing import List, Optional, Tuple
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, func, and_, or_
from sqlalchemy.orm import selectinload

from app.models.db_models import Tag, TagCategory, TagAssignment, UserTagVisibility
from app.models.schemas import (
    TagCreate, TagUpdate, TagResponse,
    TagCategoryCreate, TagCategoryUpdate, TagCategoryResponse,
    TagGrouped, ResourceTagsResponse, TagStatistics,
    TagListResponse, TagStatsResponse,
    BulkTagAssignRequest, BulkTagAssignResponse,
    UserTagVisibilitySettings, UserTagVisibilityResponse,
)
from app.core.cache import get_cache_manager
from app.services.lxd.utils import wait_for_lxd_operation
from app.core.lxd_path import safe_segment

logger = logging.getLogger(__name__)

# ============================================
# Default Tag Categories (System Tags)
# ============================================

DEFAULT_CATEGORIES = [
    {
        "prefix": "loc_",
        "name": "Location",
        "description": "Physical location or data center",
        "color": "#3b82f6",  # Blue
        "icon": "map-pin",
        "sort_order": 1,
        "is_system": True,
    },
    {
        "prefix": "reg_",
        "name": "Region",
        "description": "Geographic region or zone",
        "color": "#06b6d4",  # Cyan
        "icon": "globe",
        "sort_order": 2,
        "is_system": True,
    },
    {
        "prefix": "vend_",
        "name": "Vendor",
        "description": "Vendor or provider",
        "color": "#8b5cf6",  # Violet
        "icon": "building",
        "sort_order": 3,
        "is_system": True,
    },
    {
        "prefix": "own_",
        "name": "Ownership",
        "description": "Asset ownership or responsibility",
        "color": "#f59e0b",  # Amber
        "icon": "user-check",
        "sort_order": 4,
        "is_system": True,
    },
    {
        "prefix": "env_",
        "name": "Environment",
        "description": "Deployment environment (production, staging, dev)",
        "color": "#10b981",  # Emerald
        "icon": "layers",
        "sort_order": 5,
        "is_system": True,
    },
    {
        "prefix": "app_",
        "name": "Application",
        "description": "Application type or workload",
        "color": "#ec4899",  # Pink
        "icon": "package",
        "sort_order": 6,
        "is_system": True,
    },
    {
        "prefix": "sla_",
        "name": "SLA Level",
        "description": "Service level agreement tier",
        "color": "#ef4444",  # Red
        "icon": "shield-check",
        "sort_order": 7,
        "is_system": True,
    },
    {
        "prefix": "cost_",
        "name": "Cost Center",
        "description": "Cost center or billing code",
        "color": "#84cc16",  # Lime
        "icon": "wallet",
        "sort_order": 8,
        "is_system": True,
    },
]

# Cache TTL settings
CACHE_TTL_TAGS = 300  # 5 minutes
CACHE_TTL_CATEGORIES = 600  # 10 minutes
CACHE_TTL_STATS = 120  # 2 minutes


class TagService:
    """
    - CRUD for tags and categories
    - Tag classification based on prefix
    - Bulk operations
    - LXD sync
    - Redis caching
    - User visibility settings
    """
    
    def __init__(self, db: AsyncSession, lxd_service=None):
        self.db = db
        self.lxd_service = lxd_service
        self.cache = get_cache_manager()
    
    # ============================================
    # Tag Classification Utilities
    # ============================================
    
    @staticmethod
    def classify_tag(tag: str) -> Tuple[str, str]:
        """
        Classify a tag based on its prefix.
        """
        known_prefixes = ["loc_", "reg_", "vend_", "own_", "env_", "app_", "sla_", "cost_"]
        
        for prefix in known_prefixes:
            if tag.startswith(prefix):
                return (prefix, tag[len(prefix):])
        
        return ("other", tag)
    
    @staticmethod
    def get_display_name(tag: str) -> str:
        """
        Extract human-readable display name from tag.
        
        Examples:
            "loc_jakarta" -> "Jakarta"
            "vend_canonical" -> "Canonical"
            "production" -> "Production"
        """
        _, value = TagService.classify_tag(tag)
        # Convert to title case and replace dashes/underscores with spaces
        return value.replace("-", " ").replace("_", " ").title()
    
    @staticmethod
    def group_tags(tags: List[str]) -> TagGrouped:
        """
        Group tags by their category prefix.
        """
        grouped = TagGrouped()
        
        prefix_map = {
            "loc_": "location",
            "reg_": "region",
            "vend_": "vendor",
            "own_": "ownership",
            "env_": "environment",
            "app_": "application",
            "sla_": "sla",
            "cost_": "cost_center",
        }
        
        for tag in tags:
            prefix, value = TagService.classify_tag(tag)
            
            if prefix in prefix_map:
                category_name = prefix_map[prefix]
                getattr(grouped, category_name).append(value)
            else:
                grouped.other.append(tag)
        
        return grouped
    
    @staticmethod
    def validate_tag_name(tag: str) -> Tuple[bool, str]:
        """
        Validate tag name format.
        
        Rules:
        - Lowercase alphanumeric, dashes, underscores
        - 1-100 characters
        - Cannot start with dash or underscore (unless it's a prefix)
        """
        if not tag:
            return False, "Tag name cannot be empty"
        
        if len(tag) > 100:
            return False, "Tag name cannot exceed 100 characters"
        
        # Allow format: prefix_value or simple value
        pattern = r'^[a-z][a-z0-9_-]*$'
        if not re.match(pattern, tag):
            return False, "Tag must be lowercase, start with a letter, and contain only letters, numbers, dashes, and underscores"
        
        return True, ""
    
    # ============================================
    # Category Operations
    # ============================================
    
    async def initialize_default_categories(self) -> int:
        """
        Initialize default tag categories if they don't exist.
        Called on application startup.
        """
        created_count = 0
        
        for cat_data in DEFAULT_CATEGORIES:
            # Check if category exists
            result = await self.db.execute(
                select(TagCategory).where(TagCategory.prefix == cat_data["prefix"])
            )
            existing = result.scalar_one_or_none()
            
            if not existing:
                category = TagCategory(**cat_data)
                self.db.add(category)
                created_count += 1
                logger.info(f"Created default category: {cat_data['prefix']}")
        
        if created_count > 0:
            await self.db.commit()
            # Invalidate cache
            self.cache.delete("tags", "categories:all")
        
        return created_count
    
    async def list_categories(self, include_inactive: bool = False) -> List[TagCategoryResponse]:
        """Get all tag categories."""
        cache_key = f"categories:all:{include_inactive}"
        cached = self.cache.get("tags", cache_key)
        if cached:
            return [TagCategoryResponse(**c) for c in cached]
        
        query = select(TagCategory).order_by(TagCategory.sort_order)
        if not include_inactive:
            query = query.where(TagCategory.is_active == True)
        
        result = await self.db.execute(query)
        categories = result.scalars().all()
        
        # Get tag counts per category
        count_query = select(
            Tag.category_id,
            func.count(Tag.id).label("count")
        ).group_by(Tag.category_id)
        count_result = await self.db.execute(count_query)
        counts = {row.category_id: row.count for row in count_result}
        
        response = []
        for cat in categories:
            cat_dict = {
                "id": cat.id,
                "prefix": cat.prefix,
                "name": cat.name,
                "description": cat.description,
                "color": cat.color,
                "icon": cat.icon,
                "sort_order": cat.sort_order,
                "is_active": cat.is_active,
                "is_system": cat.is_system,
                "created_at": cat.created_at,
                "updated_at": cat.updated_at,
                "tag_count": counts.get(cat.id, 0),
            }
            response.append(TagCategoryResponse(**cat_dict))
        
        # Cache for 10 minutes
        self.cache.set("tags", cache_key, [r.model_dump() for r in response], ttl=CACHE_TTL_CATEGORIES)
        
        return response
    
    async def get_category_by_prefix(self, prefix: str) -> Optional[TagCategory]:
        """Get category by prefix."""
        result = await self.db.execute(
            select(TagCategory).where(TagCategory.prefix == prefix)
        )
        return result.scalar_one_or_none()
    
    async def create_category(self, data: TagCategoryCreate) -> TagCategoryResponse:
        """Create a new tag category."""
        # Ensure prefix ends with underscore
        prefix = data.prefix if data.prefix.endswith("_") else f"{data.prefix}_"
        
        # Check for duplicate
        existing = await self.get_category_by_prefix(prefix)
        if existing:
            raise ValueError(f"Category with prefix '{prefix}' already exists")
        
        category = TagCategory(
            prefix=prefix,
            name=data.name,
            description=data.description,
            color=data.color,
            icon=data.icon,
            sort_order=data.sort_order,
            is_active=data.is_active,
            is_system=False,  # User-created categories are not system
        )
        
        self.db.add(category)
        await self.db.commit()
        await self.db.refresh(category)
        
        # Invalidate cache
        self.cache.delete("tags", "categories:all:True")
        self.cache.delete("tags", "categories:all:False")
        
        return TagCategoryResponse(
            id=category.id,
            prefix=category.prefix,
            name=category.name,
            description=category.description,
            color=category.color,
            icon=category.icon,
            sort_order=category.sort_order,
            is_active=category.is_active,
            is_system=category.is_system,
            created_at=category.created_at,
            updated_at=category.updated_at,
            tag_count=0,
        )
    
    async def update_category(self, category_id: int, data: TagCategoryUpdate) -> TagCategoryResponse:
        """Update a tag category."""
        result = await self.db.execute(
            select(TagCategory).where(TagCategory.id == category_id)
        )
        category = result.scalar_one_or_none()
        
        if not category:
            raise ValueError(f"Category with ID {category_id} not found")
        
        # Update fields
        update_data = data.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            if hasattr(category, field):
                setattr(category, field, value)
        
        await self.db.commit()
        await self.db.refresh(category)
        
        # Invalidate cache
        self.cache.delete("tags", "categories:all:True")
        self.cache.delete("tags", "categories:all:False")
        
        # Get tag count
        count_result = await self.db.execute(
            select(func.count(Tag.id)).where(Tag.category_id == category_id)
        )
        tag_count = count_result.scalar() or 0
        
        return TagCategoryResponse(
            id=category.id,
            prefix=category.prefix,
            name=category.name,
            description=category.description,
            color=category.color,
            icon=category.icon,
            sort_order=category.sort_order,
            is_active=category.is_active,
            is_system=category.is_system,
            created_at=category.created_at,
            updated_at=category.updated_at,
            tag_count=tag_count,
        )
    
    # ============================================
    # Tag Operations
    # ============================================
    
    async def list_tags(
        self,
        category_id: Optional[int] = None,
        search: Optional[str] = None,
        user_id: Optional[str] = None,
        include_usage: bool = True,
    ) -> TagListResponse:
        """
        List all tags with optional filtering.
        """
        cache_key = f"tags:list:{category_id}:{search}:{user_id}"
        cached = self.cache.get("tags", cache_key)
        if cached and not include_usage:  # Don't cache usage counts
            return TagListResponse(**cached)
        
        query = select(Tag).options(selectinload(Tag.category))
        
        if category_id:
            query = query.where(Tag.category_id == category_id)
        
        if search:
            search_pattern = f"%{search.lower()}%"
            query = query.where(
                or_(
                    Tag.name.ilike(search_pattern),
                    Tag.display_name.ilike(search_pattern),
                )
            )
        
        # Apply user visibility filter
        if user_id:
            visibility = await self.get_user_visibility(user_id)
            if visibility and visibility.visibility_mode != "all":
                if visibility.visibility_mode == "whitelist":
                    # Only show visible tags
                    if visibility.visible_tags:
                        query = query.where(Tag.name.in_(visibility.visible_tags))
                elif visibility.visibility_mode == "blacklist":
                    # Hide specified tags
                    if visibility.hidden_tags:
                        query = query.where(~Tag.name.in_(visibility.hidden_tags))
        
        query = query.order_by(Tag.name)
        
        result = await self.db.execute(query)
        tags = result.scalars().all()
        
        # Get categories for response
        categories = await self.list_categories()
        
        # Build response
        tag_responses = []
        for tag in tags:
            cat_response = None
            if tag.category:
                cat_response = TagCategoryResponse(
                    id=tag.category.id,
                    prefix=tag.category.prefix,
                    name=tag.category.name,
                    description=tag.category.description,
                    color=tag.category.color,
                    icon=tag.category.icon,
                    sort_order=tag.category.sort_order,
                    is_active=tag.category.is_active,
                    is_system=tag.category.is_system,
                    created_at=tag.category.created_at,
                    updated_at=tag.category.updated_at,
                    tag_count=0,
                )
            
            tag_responses.append(TagResponse(
                id=tag.id,
                name=tag.name,
                display_name=tag.display_name,
                description=tag.description,
                color=tag.color or (tag.category.color if tag.category else "#6b7280"),
                category_id=tag.category_id,
                category=cat_response,
                is_system=tag.is_system,
                usage_count=tag.usage_count,
                created_by=tag.created_by,
                created_at=tag.created_at,
                updated_at=tag.updated_at,
            ))
        
        response = TagListResponse(
            tags=tag_responses,
            total=len(tag_responses),
            categories=categories,
        )
        
        # Cache without usage counts
        if not include_usage:
            self.cache.set("tags", cache_key, response.model_dump(), ttl=CACHE_TTL_TAGS)
        
        return response
    
    async def get_or_create_tag(
        self,
        tag_name: str,
        user_id: Optional[str] = None,
        auto_categorize: bool = True,
    ) -> Tag:
        """
        Get existing tag or create new one.
        """
        tag_name = tag_name.lower().strip()
        
        # Validate
        is_valid, error = self.validate_tag_name(tag_name)
        if not is_valid:
            raise ValueError(error)
        
        # Check existing
        result = await self.db.execute(
            select(Tag).where(Tag.name == tag_name)
        )
        existing = result.scalar_one_or_none()
        
        if existing:
            return existing
        
        # Create new tag
        category_id = None
        display_name = self.get_display_name(tag_name)
        
        if auto_categorize:
            prefix, _ = self.classify_tag(tag_name)
            if prefix != "other":
                category = await self.get_category_by_prefix(prefix)
                if category:
                    category_id = category.id
        
        tag = Tag(
            name=tag_name,
            display_name=display_name,
            category_id=category_id,
            created_by=user_id,
        )
        
        self.db.add(tag)
        await self.db.commit()
        await self.db.refresh(tag)
        
        logger.info(f"Created new tag: {tag_name} (category_id={category_id})")
        
        # Invalidate cache
        self._invalidate_tag_cache()
        
        return tag
    
    async def create_tag(self, data: TagCreate, user_id: Optional[str] = None) -> TagResponse:
        """Create a new tag explicitly."""
        tag_name = data.name.lower().strip()
        
        # Validate
        is_valid, error = self.validate_tag_name(tag_name)
        if not is_valid:
            raise ValueError(error)
        
        # Check duplicate
        result = await self.db.execute(
            select(Tag).where(Tag.name == tag_name)
        )
        if result.scalar_one_or_none():
            raise ValueError(f"Tag '{tag_name}' already exists")
        
        # Determine category
        category_id = data.category_id
        if not category_id:
            prefix, _ = self.classify_tag(tag_name)
            if prefix != "other":
                category = await self.get_category_by_prefix(prefix)
                if category:
                    category_id = category.id
        
        # Create tag
        tag = Tag(
            name=tag_name,
            display_name=data.display_name or self.get_display_name(tag_name),
            description=data.description,
            color=data.color,
            category_id=category_id,
            created_by=user_id,
        )
        
        self.db.add(tag)
        await self.db.commit()
        await self.db.refresh(tag)
        
        # Load category
        if tag.category_id:
            await self.db.refresh(tag, ["category"])
        
        self._invalidate_tag_cache()
        
        return self._tag_to_response(tag)
    
    async def update_tag(self, tag_id: int, data: TagUpdate) -> TagResponse:
        """Update a tag."""
        result = await self.db.execute(
            select(Tag).options(selectinload(Tag.category)).where(Tag.id == tag_id)
        )
        tag = result.scalar_one_or_none()
        
        if not tag:
            raise ValueError(f"Tag with ID {tag_id} not found")
        
        update_data = data.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            if hasattr(tag, field):
                setattr(tag, field, value)
        
        await self.db.commit()
        await self.db.refresh(tag)
        
        self._invalidate_tag_cache()
        
        return self._tag_to_response(tag)
    
    async def delete_tag(self, tag_id: int) -> bool:
        """Delete a tag and all its assignments."""
        result = await self.db.execute(
            select(Tag).where(Tag.id == tag_id)
        )
        tag = result.scalar_one_or_none()
        
        if not tag:
            raise ValueError(f"Tag with ID {tag_id} not found")
        
        if tag.is_system:
            raise ValueError("Cannot delete system tags")
        
        await self.db.delete(tag)
        await self.db.commit()
        
        self._invalidate_tag_cache()
        
        logger.info(f"Deleted tag: {tag.name}")
        return True
    
    # ============================================
    # Tag Assignment Operations
    # ============================================
    
    async def assign_tags_to_resource(
        self,
        request: BulkTagAssignRequest,
        user_id: Optional[str] = None,
        sync_to_lxd: bool = True,
    ) -> BulkTagAssignResponse:
        """
        Assign multiple tags to a resource.
        
        This is the main method for tag assignment that:
        1. Creates tags if they don't exist
        2. Creates assignments in PostgreSQL
        3. Syncs to LXD for backward compatibility
        """
        assigned_tags = []
        failed_tags = []
        
        for tag_name in request.tags:
            try:
                tag_name = tag_name.lower().strip()
                if not tag_name:
                    continue
                
                # Get or create tag
                tag = await self.get_or_create_tag(tag_name, user_id)
                
                # Check if assignment exists
                existing_query = select(TagAssignment).where(
                    and_(
                        TagAssignment.tag_id == tag.id,
                        TagAssignment.resource_type == request.resource_type,
                        TagAssignment.resource_id == request.resource_id,
                        TagAssignment.resource_host == request.resource_host,
                        TagAssignment.resource_project == request.resource_project,
                    )
                )
                existing = (await self.db.execute(existing_query)).scalar_one_or_none()
                
                if not existing:
                    assignment = TagAssignment(
                        tag_id=tag.id,
                        resource_type=request.resource_type,
                        resource_id=request.resource_id,
                        resource_host=request.resource_host,
                        resource_project=request.resource_project,
                        assigned_by=user_id,
                    )
                    self.db.add(assignment)
                    
                    # Update usage count
                    tag.usage_count += 1
                
                assigned_tags.append(tag_name)
                
            except Exception as e:
                logger.error(f"Failed to assign tag '{tag_name}': {e}")
                failed_tags.append(tag_name)
        
        await self.db.commit()
        
        # Sync to LXD
        synced = False
        if sync_to_lxd and self.lxd_service and assigned_tags:
            try:
                synced = await self._sync_tags_to_lxd(
                    request.resource_type,
                    request.resource_id,
                    request.resource_host,
                    request.resource_project,
                )
            except Exception as e:
                logger.warning(f"Failed to sync tags to LXD: {e}")
        
        self._invalidate_tag_cache()
        self._invalidate_resource_cache(request.resource_type)
        
        return BulkTagAssignResponse(
            success=len(failed_tags) == 0,
            message=f"Assigned {len(assigned_tags)} tags" + (f", {len(failed_tags)} failed" if failed_tags else ""),
            assigned_tags=assigned_tags,
            failed_tags=failed_tags,
            synced_to_lxd=synced,
        )
    
    async def remove_tag_from_resource(
        self,
        tag_name: str,
        resource_type: str,
        resource_id: str,
        resource_host: Optional[str] = None,
        resource_project: Optional[str] = None,
        sync_to_lxd: bool = True,
    ) -> bool:
        """Remove a single tag from a resource."""
        tag_name = tag_name.lower().strip()
        
        # Find tag
        tag_result = await self.db.execute(
            select(Tag).where(Tag.name == tag_name)
        )
        tag = tag_result.scalar_one_or_none()
        
        if not tag:
            return False
        
        # Find assignment
        query = select(TagAssignment).where(
            and_(
                TagAssignment.tag_id == tag.id,
                TagAssignment.resource_type == resource_type,
                TagAssignment.resource_id == resource_id,
                TagAssignment.resource_host == resource_host,
                TagAssignment.resource_project == resource_project,
            )
        )
        result = await self.db.execute(query)
        assignment = result.scalar_one_or_none()
        
        if assignment:
            await self.db.delete(assignment)
            tag.usage_count = max(0, tag.usage_count - 1)
            await self.db.commit()
            
            # Sync to LXD
            if sync_to_lxd and self.lxd_service:
                try:
                    await self._sync_tags_to_lxd(
                        resource_type, resource_id, resource_host, resource_project
                    )
                except Exception as e:
                    logger.warning(f"Failed to sync tags to LXD: {e}")
            
            self._invalidate_tag_cache()
            self._invalidate_resource_cache(resource_type)
            return True
        
        return False
    
    async def set_resource_tags(
        self,
        resource_type: str,
        resource_id: str,
        tags: List[str],
        resource_host: Optional[str] = None,
        resource_project: Optional[str] = None,
        user_id: Optional[str] = None,
        sync_to_lxd: bool = True,
    ) -> BulkTagAssignResponse:
        """
        Set exact tags for a resource (replace all existing).
        
        This removes all current tags and assigns the new ones.
        """
        # Remove all existing assignments
        delete_query = delete(TagAssignment).where(
            and_(
                TagAssignment.resource_type == resource_type,
                TagAssignment.resource_id == resource_id,
                TagAssignment.resource_host == resource_host,
                TagAssignment.resource_project == resource_project,
            )
        )
        await self.db.execute(delete_query)
        await self.db.commit()
        
        # Assign new tags
        if tags:
            return await self.assign_tags_to_resource(
                BulkTagAssignRequest(
                    tags=tags,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    resource_host=resource_host,
                    resource_project=resource_project,
                ),
                user_id=user_id,
                sync_to_lxd=sync_to_lxd,
            )
        
        # Sync empty tags to LXD
        if sync_to_lxd and self.lxd_service:
            try:
                await self._sync_tags_to_lxd(
                    resource_type, resource_id, resource_host, resource_project
                )
            except Exception as e:
                logger.warning(f"Failed to sync tags to LXD: {e}")
        
        # Invalidate cache for resource type (ex; projects list)
        self._invalidate_resource_cache(resource_type)
        
        return BulkTagAssignResponse(
            success=True,
            message="All tags removed",
            assigned_tags=[],
            synced_to_lxd=True,
        )
    
    async def get_resource_tags(
        self,
        resource_type: str,
        resource_id: str,
        resource_host: Optional[str] = None,
        resource_project: Optional[str] = None,
    ) -> ResourceTagsResponse:
        """Get all tags for a resource with grouping."""
        query = (
            select(TagAssignment)
            .options(selectinload(TagAssignment.tag).selectinload(Tag.category))
            .where(
                and_(
                    TagAssignment.resource_type == resource_type,
                    TagAssignment.resource_id == resource_id,
                    TagAssignment.resource_host == resource_host,
                    TagAssignment.resource_project == resource_project,
                )
            )
        )
        
        result = await self.db.execute(query)
        assignments = result.scalars().all()
        
        tags = [a.tag.name for a in assignments if a.tag]
        grouped = self.group_tags(tags)
        
        return ResourceTagsResponse(
            resource_type=resource_type,
            resource_id=resource_id,
            resource_host=resource_host,
            resource_project=resource_project,
            tags=tags,
            grouped=grouped,
        )
    
    # ============================================
    # User Visibility Settings
    # ============================================
    
    async def get_user_visibility(self, user_id: str) -> Optional[UserTagVisibility]:
        """Get user's tag visibility settings."""
        result = await self.db.execute(
            select(UserTagVisibility).where(UserTagVisibility.user_id == user_id)
        )
        return result.scalar_one_or_none()
    
    async def get_user_visibility_response(self, user_id: str) -> UserTagVisibilityResponse:
        """Get user visibility settings as response object."""
        visibility = await self.get_user_visibility(user_id)
        
        if visibility:
            return UserTagVisibilityResponse(
                user_id=visibility.user_id,
                visibility_mode=visibility.visibility_mode,
                visible_categories=visibility.visible_categories or [],
                visible_tags=visibility.visible_tags or [],
                hidden_categories=visibility.hidden_categories or [],
                hidden_tags=visibility.hidden_tags or [],
                default_filter_tags=visibility.default_filter_tags or [],
                updated_at=visibility.updated_at,
            )
        
        # Return defaults
        return UserTagVisibilityResponse(
            user_id=user_id,
            visibility_mode="all",
            visible_categories=[],
            visible_tags=[],
            hidden_categories=[],
            hidden_tags=[],
            default_filter_tags=[],
            updated_at=datetime.now(timezone.utc),
        )
    
    async def update_user_visibility(
        self,
        user_id: str,
        settings: UserTagVisibilitySettings,
    ) -> UserTagVisibilityResponse:
        """Update user's tag visibility settings."""
        visibility = await self.get_user_visibility(user_id)
        
        if visibility:
            # Update existing
            visibility.visibility_mode = settings.visibility_mode
            visibility.visible_categories = settings.visible_categories
            visibility.visible_tags = settings.visible_tags
            visibility.hidden_categories = settings.hidden_categories
            visibility.hidden_tags = settings.hidden_tags
            visibility.default_filter_tags = settings.default_filter_tags
        else:
            # Create new
            visibility = UserTagVisibility(
                user_id=user_id,
                visibility_mode=settings.visibility_mode,
                visible_categories=settings.visible_categories,
                visible_tags=settings.visible_tags,
                hidden_categories=settings.hidden_categories,
                hidden_tags=settings.hidden_tags,
                default_filter_tags=settings.default_filter_tags,
            )
            self.db.add(visibility)
        
        await self.db.commit()
        await self.db.refresh(visibility)
        
        return await self.get_user_visibility_response(user_id)
    
    # ============================================
    # Statistics & Analytics
    # ============================================
    
    async def get_tag_statistics(self) -> TagStatsResponse:
        """Get comprehensive tag statistics."""
        cache_key = "stats:overview"
        cached = self.cache.get("tags", cache_key)
        if cached:
            return TagStatsResponse(**cached)
        
        # Total tags
        total_tags = (await self.db.execute(select(func.count(Tag.id)))).scalar() or 0
        
        # Total assignments
        total_assignments = (await self.db.execute(select(func.count(TagAssignment.id)))).scalar() or 0
        
        # Tags by category
        category_counts = await self.db.execute(
            select(
                TagCategory.prefix,
                func.count(Tag.id).label("count")
            )
            .outerjoin(Tag, Tag.category_id == TagCategory.id)
            .group_by(TagCategory.prefix)
        )
        tags_by_category = {row.prefix: row.count for row in category_counts}
        
        # Top tags
        top_tags_query = (
            select(Tag)
            .options(selectinload(Tag.category))
            .order_by(Tag.usage_count.desc())
            .limit(10)
        )
        top_tags_result = await self.db.execute(top_tags_query)
        top_tags = top_tags_result.scalars().all()
        
        top_stats = []
        for tag in top_tags:
            # Get counts by resource type
            node_count = (await self.db.execute(
                select(func.count(TagAssignment.id))
                .where(and_(TagAssignment.tag_id == tag.id, TagAssignment.resource_type == "node"))
            )).scalar() or 0
            
            project_count = (await self.db.execute(
                select(func.count(TagAssignment.id))
                .where(and_(TagAssignment.tag_id == tag.id, TagAssignment.resource_type == "project"))
            )).scalar() or 0
            
            instance_count = (await self.db.execute(
                select(func.count(TagAssignment.id))
                .where(and_(TagAssignment.tag_id == tag.id, TagAssignment.resource_type == "instance"))
            )).scalar() or 0
            
            top_stats.append(TagStatistics(
                tag_name=tag.name,
                display_name=tag.display_name,
                category=tag.category.prefix if tag.category else None,
                color=tag.color or (tag.category.color if tag.category else "#6b7280"),
                node_count=node_count,
                project_count=project_count,
                instance_count=instance_count,
                total_usage=tag.usage_count,
            ))
        
        # Get categories
        categories = await self.list_categories()
        
        response = TagStatsResponse(
            total_tags=total_tags,
            total_assignments=total_assignments,
            categories=categories,
            top_tags=top_stats,
            tags_by_category=tags_by_category,
        )
        
        self.cache.set("tags", cache_key, response.model_dump(), ttl=CACHE_TTL_STATS)
        
        return response
    
    # ============================================
    # LXD Sync
    # ============================================
    
    async def _sync_tags_to_lxd(
        self,
        resource_type: str,
        resource_id: str,
        resource_host: Optional[str] = None,
        resource_project: Optional[str] = None,
    ) -> bool:
        """
        Sync tags from PostgreSQL to LXD's user.tags config.
        
        This maintains backward compatibility with LXD's native tag storage.
        """
        if not self.lxd_service:
            return False
        
        # Get current tags from PostgreSQL
        resource_tags = await self.get_resource_tags(
            resource_type, resource_id, resource_host, resource_project
        )
        tags_str = ",".join(resource_tags.tags)
        
        try:
            if resource_type == "node":
                client = self.lxd_service.connection_manager.get_client(resource_id)
                response = client.api.get()
                server_data = response.json()["metadata"]
                if "config" not in server_data:
                    server_data["config"] = {}
                server_data["config"]["user.tags"] = tags_str
                client.api.put(json={"config": server_data["config"]})
                
            elif resource_type == "project":
                client = self.lxd_service.connection_manager.get_client(resource_host)
                project = client.projects.get(safe_segment(resource_id))
                project.config["user.tags"] = tags_str
                project.save()
                
            elif resource_type == "instance":
                client = self.lxd_service.connection_manager.get_client(resource_host)
                response = client.api.instances[safe_segment(resource_id)].get(
                    params={'project': resource_project or 'default'}
                )
                instance_data = response.json()['metadata']
                if 'config' not in instance_data:
                    instance_data['config'] = {}
                instance_data['config']['user.tags'] = tags_str
                put_response = client.api.instances[safe_segment(resource_id)].put(
                    json=instance_data,
                    params={'project': resource_project or 'default'}
                )
                # important: an instance update is asynchronous. LXD answers 202 and does the work
                # afterwards, so returning here reported synced_to_lxd=true for a write that could
                # still fail: the instance mid-operation, the config rejected, the daemon
                # restarted. The caller shows the tag as live on the cluster while LXD never
                # applied it, and nothing revisits the discrepancy.
                wait_for_lxd_operation(client, put_response)

            logger.info(f"Synced tags to LXD for {resource_type}:{resource_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to sync tags to LXD: {e}")
            return False
    
    async def import_tags_from_lxd(
        self,
        resource_type: str,
        resource_id: str,
        resource_host: Optional[str] = None,
        resource_project: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> BulkTagAssignResponse:
        """
        Import tags from LXD's user.tags into PostgreSQL.
        
        Useful for migrating existing LXD tags to the new system.
        """
        if not self.lxd_service:
            return BulkTagAssignResponse(
                success=False,
                message="LXD service not available",
                assigned_tags=[],
            )
        
        tags_str = ""
        
        try:
            if resource_type == "node":
                client = self.lxd_service.connection_manager.get_client(resource_id)
                response = client.api.get()
                server_data = response.json()["metadata"]
                tags_str = server_data.get("config", {}).get("user.tags", "")
                
            elif resource_type == "project":
                client = self.lxd_service.connection_manager.get_client(resource_host)
                project = client.projects.get(safe_segment(resource_id))
                tags_str = project.config.get("user.tags", "")
                
            elif resource_type == "instance":
                client = self.lxd_service.connection_manager.get_client(resource_host)
                response = client.api.instances[safe_segment(resource_id)].get(
                    params={'project': resource_project or 'default'}
                )
                instance_data = response.json()['metadata']
                tags_str = instance_data.get("config", {}).get("user.tags", "")
            
        except Exception as e:
            logger.error(f"Failed to get tags from LXD: {e}")
            return BulkTagAssignResponse(
                success=False,
                message=f"Failed to get tags from LXD: {e}",
                assigned_tags=[],
            )
        
        if not tags_str:
            return BulkTagAssignResponse(
                success=True,
                message="No tags found in LXD",
                assigned_tags=[],
            )
        
        tags = [t.strip() for t in tags_str.split(",") if t.strip()]
        
        return await self.assign_tags_to_resource(
            BulkTagAssignRequest(
                tags=tags,
                resource_type=resource_type,
                resource_id=resource_id,
                resource_host=resource_host,
                resource_project=resource_project,
            ),
            user_id=user_id,
            sync_to_lxd=False,  # Don't sync back, we're importing
        )
    
    # ============================================
    # Helper Methods
    # ============================================
    
    def _tag_to_response(self, tag: Tag) -> TagResponse:
        """Convert Tag model to TagResponse."""
        cat_response = None
        if tag.category:
            cat_response = TagCategoryResponse(
                id=tag.category.id,
                prefix=tag.category.prefix,
                name=tag.category.name,
                description=tag.category.description,
                color=tag.category.color,
                icon=tag.category.icon,
                sort_order=tag.category.sort_order,
                is_active=tag.category.is_active,
                is_system=tag.category.is_system,
                created_at=tag.category.created_at,
                updated_at=tag.category.updated_at,
                tag_count=0,
            )
        
        return TagResponse(
            id=tag.id,
            name=tag.name,
            display_name=tag.display_name,
            description=tag.description,
            color=tag.color or (tag.category.color if tag.category else "#6b7280"),
            category_id=tag.category_id,
            category=cat_response,
            is_system=tag.is_system,
            usage_count=tag.usage_count,
            created_by=tag.created_by,
            created_at=tag.created_at,
            updated_at=tag.updated_at,
        )
    
    async def remove_resource_assignments(
        self,
        resource_type: str,
        resource_id: str,
        resource_host: Optional[str] = None,
        resource_project: Optional[str] = None,
    ) -> int:
        """Remove all tag assignments for a resource (e.g. when an instance is
        deleted) and recompute usage_count for the affected tags so the Postgres
        side doesn't drift from reality. Returns the number of affected tags.
        """
        match = and_(
            TagAssignment.resource_type == resource_type,
            TagAssignment.resource_id == resource_id,
            TagAssignment.resource_host == resource_host,
            TagAssignment.resource_project == resource_project,
        )
        rows = await self.db.execute(select(TagAssignment.tag_id).where(match))
        tag_ids = {row[0] for row in rows.all()}
        if not tag_ids:
            return 0

        await self.db.execute(delete(TagAssignment).where(match))

        # Recompute usage_count from the surviving assignments (robust against any
        # prior drift, not just a decrement).
        for tag_id in tag_ids:
            remaining = (
                await self.db.execute(
                    select(func.count(TagAssignment.id)).where(
                        TagAssignment.tag_id == tag_id
                    )
                )
            ).scalar() or 0
            tag = await self.db.get(Tag, tag_id)
            if tag:
                tag.usage_count = remaining

        await self.db.commit()
        self._invalidate_tag_cache()
        self._invalidate_resource_cache(resource_type)
        return len(tag_ids)

    def _invalidate_tag_cache(self):
        """Invalidate all tag-related caches."""
        # Use delete_pattern (glob/prefix match), not delete (single literal key),
        # otherwise the trailing "*" never matches the real keys.
        self.cache.delete_pattern("tags", "tags:list:*")
        self.cache.delete_pattern("tags", "categories:all:*")
        self.cache.delete_pattern("tags", "stats:*")

    def _invalidate_resource_cache(self, resource_type: str):
        """Invalidate resource-related caches after tag operations."""
        if resource_type == "project":
            self.cache.delete_pattern("api_responses", "projects:*")
        elif resource_type == "instance":
            self.cache.delete_pattern("api_responses", "vms:*")
            self.cache.delete_pattern("api_responses", "instances:*")
        elif resource_type == "node":
            self.cache.delete_pattern("api_responses", "nodes:*")


# ============================================
# Service Factory
# ============================================

async def get_tag_service(
    db: AsyncSession,
    lxd_service=None,
) -> TagService:
    """Factory function to create TagService instance."""
    return TagService(db=db, lxd_service=lxd_service)
