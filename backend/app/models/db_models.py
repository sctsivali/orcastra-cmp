"""
Database Models for Orcastra Dashboard.
SQLAlchemy ORM models for PostgreSQL.
"""

from datetime import datetime, timezone
from sqlalchemy import Column, String, Text, DateTime, Index, Integer, Boolean, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, ARRAY
from sqlalchemy.orm import relationship
from app.core.database import Base


# ============================================
# Tag System Models
# ============================================

class TagCategory(Base):
    """
    Tag category/prefix definitions.
    
    Predefined categories:
    - loc_  : Location-based (ex; loc_jakarta, loc_surabaya)
    - reg_  : Region-based (ex; reg_jawa-timur, reg_kalimantan)
    - vend_ : Vendor (ex; vend_canonical, vend_ecobytes)
    - own_  : Ownership (ex; own_sivali, own_partner)
    - env_  : Environment (ex; env_production, env_staging)
    - app_  : Application type (ex; app_banking, app_hci)
    - sla_  : SLA level (ex; sla_critical, sla_standard)
    - cost_ : Cost center (ex; cost_dept-it, cost_project-a)
    """
    __tablename__ = "tag_categories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    prefix = Column(String(10), unique=True, nullable=False, index=True)  # loc_, vend_, etc.
    name = Column(String(50), nullable=False)  # "Location", "Vendor"
    description = Column(Text, nullable=True)
    color = Column(String(7), nullable=True, default="#6b7280")  # Hex color for UI
    icon = Column(String(50), nullable=True, default="tag")  # Lucide icon name
    sort_order = Column(Integer, default=0)  # For ordering in UI
    is_active = Column(Boolean, default=True)
    is_system = Column(Boolean, default=True)  # System categories cannot be deleted
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False
    )

    # Relationships
    tags = relationship("Tag", back_populates="category", lazy="selectin")

    def __repr__(self) -> str:
        return f"<TagCategory(prefix={self.prefix}, name={self.name})>"


class Tag(Base):
    """
    Master tag definitions.
    
    Tags follow the format: {prefix}{value}
    Example: loc_jakarta, vend_canonical, production
    
    Tags without recognized prefix are categorized as "other".
    """
    __tablename__ = "tags"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False, index=True)  # Full tag: loc_jakarta
    display_name = Column(String(100), nullable=True)  # Human-readable: "Jakarta"
    category_id = Column(Integer, ForeignKey("tag_categories.id", ondelete="SET NULL"), nullable=True)
    description = Column(Text, nullable=True)
    color = Column(String(7), nullable=True)  # Override category color if needed
    is_system = Column(Boolean, default=False)  # System tags cannot be deleted by users
    usage_count = Column(Integer, default=0)  # Denormalized count for performance
    created_by = Column(String(255), nullable=True)  # User ID who created
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False
    )

    # Relationships
    category = relationship("TagCategory", back_populates="tags", lazy="joined")
    assignments = relationship("TagAssignment", back_populates="tag", lazy="selectin", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_tags_category_id", "category_id"),
        Index("ix_tags_created_by", "created_by"),
    )

    def __repr__(self) -> str:
        return f"<Tag(name={self.name}, category_id={self.category_id})>"


class TagAssignment(Base):
    """
    Tag assignments to resources (nodes, projects, instances).
    
    This table links tags to LXD resources and tracks who assigned them.
    Also syncs to LXD's user.tags config for backward compatibility.
    """
    __tablename__ = "tag_assignments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tag_id = Column(Integer, ForeignKey("tags.id", ondelete="CASCADE"), nullable=False)
    
    # Resource identification
    resource_type = Column(String(20), nullable=False, index=True)  # node, project, instance
    resource_id = Column(String(255), nullable=False)  # node_name, project_name, instance_name
    resource_host = Column(String(255), nullable=True)  # LXD host for project/instance
    resource_project = Column(String(255), nullable=True)  # LXD project for instances
    
    # Audit trail
    assigned_by = Column(String(255), nullable=True)  # User ID who assigned
    assigned_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )

    # Relationships
    tag = relationship("Tag", back_populates="assignments", lazy="joined")

    __table_args__ = (
        # Ensure unique tag per resource
        UniqueConstraint(
            "tag_id", "resource_type", "resource_id", "resource_host", "resource_project",
            name="uq_tag_assignment_resource"
        ),
        Index("ix_tag_assignment_resource", "resource_type", "resource_id"),
        Index("ix_tag_assignment_host", "resource_host"),
        Index("ix_tag_assignment_project", "resource_project"),
    )

    def __repr__(self) -> str:
        return f"<TagAssignment(tag_id={self.tag_id}, resource={self.resource_type}:{self.resource_id})>"


class UserTagVisibility(Base):
    """
    Per-user tag visibility settings.
    
    Controls which tags a user can see/filter in the dashboard.
    This enables personalized views where users only see relevant tags.
    
    By default, users can see all tags. This table stores exceptions:
    - If visibility_mode = 'whitelist': only see tags in visible_tags
    - If visibility_mode = 'blacklist': see all except tags in hidden_tags
    """
    __tablename__ = "user_tag_visibility"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(255), nullable=False, index=True)
    
    # Visibility mode: 'all', 'whitelist', 'blacklist'
    visibility_mode = Column(String(20), default="all", nullable=False)
    
    # For whitelist mode: only these categories/tags are visible
    visible_categories = Column(ARRAY(String), default=[], nullable=False)  # List of category prefixes
    visible_tags = Column(ARRAY(String), default=[], nullable=False)  # List of specific tag names
    
    # For blacklist mode: hide these categories/tags
    hidden_categories = Column(ARRAY(String), default=[], nullable=False)
    hidden_tags = Column(ARRAY(String), default=[], nullable=False)
    
    # User's preferred default filter (saved filter state)
    default_filter_tags = Column(ARRAY(String), default=[], nullable=False)
    
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False
    )

    __table_args__ = (
        UniqueConstraint("user_id", name="uq_user_tag_visibility"),
    )

    def __repr__(self) -> str:
        return f"<UserTagVisibility(user_id={self.user_id}, mode={self.visibility_mode})>"


# ============================================
# User Preferences Model
# ============================================

class UserPreferences(Base):
    """
    User preferences/personalization settings.
    Stores per-user customization data including branding, colors, logos, etc.
    """
    __tablename__ = "user_preferences"

    # Primary key: user ID from Authentik (OIDC sub claim)
    user_id = Column(String(255), primary_key=True, index=True)
    
    # User email for reference (not used for lookup)
    user_email = Column(String(255), nullable=True)
    
    # JSONB column to store all personalization settings flexibly
    # This allows schema-less storage of the PersonalizationSettings object
    settings = Column(JSONB, nullable=False, default={})
    
    # Timestamps
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False
    )

    # Index for faster lookups
    __table_args__ = (
        Index("ix_user_preferences_email", "user_email"),
    )

    def __repr__(self) -> str:
        return f"<UserPreferences(user_id={self.user_id}, email={self.user_email})>"
