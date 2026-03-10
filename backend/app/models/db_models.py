"""
Database Models for Orcastra Dashboard.
SQLAlchemy ORM models for PostgreSQL.
"""

from datetime import datetime, timezone
from sqlalchemy import Column, String, Text, DateTime, Index, Integer, BigInteger, Boolean, ForeignKey, UniqueConstraint
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


# ============================================
# Organization & RBAC Models (Three-Tier)
# ============================================

class Organization(Base):
    """
    Organization / workspace scoping entity.

    Partners create organizations and register clusters into them.
    Tenants are assigned to organizations to access those clusters.
    Admin has visibility into all organizations.

    source values:
      - 'local': created directly in Orcastra Dashboard
      - 'orcahub': synced from OrcaHub via API key integration
    """
    __tablename__ = "organizations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    slug = Column(String(100), unique=True, nullable=False, index=True)
    description = Column(Text, nullable=True)
    # email of the partner who created the org (or 'system' for synced orgs)
    created_by = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True)

    # external source tracking
    source = Column(String(50), nullable=False, default="local")  # local | orcahub
    external_id = Column(String(255), nullable=True)  # UUID from OrcaHub
    api_key_id = Column(String(255), nullable=True, index=True)  # which API key synced this

    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    members = relationship("OrganizationMember", back_populates="organization", lazy="selectin", cascade="all, delete-orphan")
    clusters = relationship("OrganizationCluster", back_populates="organization", lazy="selectin", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Organization(slug={self.slug}, name={self.name})>"


class OrganizationMember(Base):
    """
    Membership link between a user (by email) and an organization.
    The role column is kept for DB compatibility but is not used for access control.
    All authorization is based on system-level roles (admin/partner/tenant) from the IdP.
    """
    __tablename__ = "organization_members"

    id = Column(Integer, primary_key=True, autoincrement=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    user_email = Column(String(255), nullable=False, index=True)
    role = Column(String(20), nullable=False, default="member")
    invited_by = Column(String(255), nullable=True)

    joined_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    organization = relationship("Organization", back_populates="members")

    __table_args__ = (
        UniqueConstraint("organization_id", "user_email", name="uq_org_member_email"),
        Index("ix_org_member_org_id", "organization_id"),
    )

    def __repr__(self) -> str:
        return f"<OrganizationMember(org={self.organization_id}, email={self.user_email}, role={self.role})>"


class OrganizationCluster(Base):
    """
    Binds a Vault-stored cluster to an organization.

    cluster_id references the Vault KV key under secret/data/clusters/{cluster_id}.
    Partners register clusters and bind them to their organization.
    """
    __tablename__ = "organization_clusters"

    id = Column(Integer, primary_key=True, autoincrement=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    cluster_id = Column(String(255), nullable=False)  # vault key
    registered_by = Column(String(255), nullable=True)

    registered_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    organization = relationship("Organization", back_populates="clusters")

    __table_args__ = (
        UniqueConstraint("organization_id", "cluster_id", name="uq_org_cluster"),
        Index("ix_org_cluster_org_id", "organization_id"),
        Index("ix_org_cluster_cluster_id", "cluster_id"),
    )

    def __repr__(self) -> str:
        return f"<OrganizationCluster(org={self.organization_id}, cluster={self.cluster_id})>"


class OrganizationPreferences(Base):
    """
    Per-organization personalization (branding, logo, colors, background).

    Partners manage this for their org.
    Tenants in the org auto-inherit these settings.
    Structure mirrors UserPreferences.settings JSONB but is org-scoped.
    """
    __tablename__ = "organization_preferences"

    id = Column(Integer, primary_key=True, autoincrement=True)
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    # JSONB stores the full PersonalizationSettings object
    settings = Column(JSONB, nullable=False, default={})
    updated_by = Column(String(255), nullable=True)

    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    organization = relationship("Organization", backref="preferences", uselist=False)

    def __repr__(self) -> str:
        return f"<OrganizationPreferences(org={self.organization_id})>"


# ============================================
# Tenant Access Policy Models (ACL)
# ============================================

class TenantAccessPolicy(Base):
    """
    Reusable access policy for tenant users.

    Follows the AWS IAM / Azure RBAC pattern:
    1. Admin/Partner creates named policies with rules.
    2. Policies are assigned to organization members (tenants).
    3. Tenants inherit cluster/project/instance permissions from assigned policies.

    source values:
      - 'local': created in Orcastra Dashboard
      - 'orcahub': synced from OrcaHub via API integration
    """
    __tablename__ = "tenant_access_policies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    is_default = Column(Boolean, default=False)  # auto-assign to new tenants in org
    source = Column(String(50), nullable=False, default="local")
    external_id = Column(String(255), nullable=True)
    created_by = Column(String(255), nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    organization = relationship("Organization", backref="access_policies")
    rules = relationship(
        "TenantPolicyRule",
        back_populates="policy",
        lazy="selectin",
        cascade="all, delete-orphan",
    )
    assignments = relationship(
        "TenantPolicyAssignment",
        back_populates="policy",
        lazy="selectin",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_tenant_policy_org_name"),
        Index("ix_tenant_policy_org_id", "organization_id"),
        Index("ix_tenant_policy_external_id", "external_id"),
    )

    def __repr__(self) -> str:
        return f"<TenantAccessPolicy(id={self.id}, name={self.name}, org={self.organization_id})>"


class TenantPolicyRule(Base):
    """
    A single access rule within a policy.

    Defines what resources (cluster/project/instance) the policy grants access to
    and what permissions are allowed.

    Supports wildcards:
      - project_name='*' -> all projects in the cluster
      - instance_name='*' -> all instances in the project
      - Both can be specific names for granular control

    Permissions: ['view', 'operate', 'console', 'snapshot', 'file_transfer']
    """
    __tablename__ = "tenant_policy_rules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    policy_id = Column(
        Integer,
        ForeignKey("tenant_access_policies.id", ondelete="CASCADE"),
        nullable=False,
    )
    cluster_id = Column(String(255), nullable=False)
    project_name = Column(String(255), nullable=False, default="*")
    instance_name = Column(String(255), nullable=False, default="*")
    permissions = Column(JSONB, nullable=False, default=["view"])
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    policy = relationship("TenantAccessPolicy", back_populates="rules")

    __table_args__ = (
        UniqueConstraint(
            "policy_id", "cluster_id", "project_name", "instance_name",
            name="uq_policy_rule_scope",
        ),
        Index("ix_policy_rule_policy_id", "policy_id"),
        Index("ix_policy_rule_cluster_id", "cluster_id"),
    )

    def __repr__(self) -> str:
        return f"<TenantPolicyRule(policy={self.policy_id}, cluster={self.cluster_id}, project={self.project_name})>"


class TenantPolicyAssignment(Base):
    """
    Links a policy to an organization member (tenant user).

    A tenant can have multiple policies assigned (permissions are merged/unioned).
    Supports expiration for temporary access grants.

    source values:
      - 'local': assigned via Orcastra Dashboard
      - 'orcahub': synced from OrcaHub
    """
    __tablename__ = "tenant_policy_assignments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    policy_id = Column(
        Integer,
        ForeignKey("tenant_access_policies.id", ondelete="CASCADE"),
        nullable=False,
    )
    org_member_id = Column(
        Integer,
        ForeignKey("organization_members.id", ondelete="CASCADE"),
        nullable=False,
    )
    assigned_by = Column(String(255), nullable=False)
    assigned_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    expires_at = Column(DateTime(timezone=True), nullable=True)
    source = Column(String(50), nullable=False, default="local")
    external_id = Column(String(255), nullable=True)

    # Relationships
    policy = relationship("TenantAccessPolicy", back_populates="assignments")
    member = relationship("OrganizationMember", backref="policy_assignments")

    __table_args__ = (
        UniqueConstraint("policy_id", "org_member_id", name="uq_policy_assignment"),
        Index("ix_policy_assign_policy_id", "policy_id"),
        Index("ix_policy_assign_member_id", "org_member_id"),
        Index("ix_policy_assign_external_id", "external_id"),
    )

    def __repr__(self) -> str:
        return f"<TenantPolicyAssignment(policy={self.policy_id}, member={self.org_member_id})>"


# ============================================
# Session Audit Logging
# ============================================

class SessionAuditLog(Base):
    """
    Persistent audit log for console/terminal session events.
    
    Records session lifecycle events for compliance and audit:
    - connect: User started a console/terminal session
    - disconnect: Session ended (includes duration & bandwidth totals)
    
    Data is immutable once written - append-only for audit integrity.
    """
    __tablename__ = "session_audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(100), nullable=False, index=True, comment="Backend session tracker ID")
    event_type = Column(String(20), nullable=False, comment="connect | disconnect")
    
    # Instance context
    instance_name = Column(String(200), nullable=False, index=True)
    host = Column(String(200), nullable=False)
    project = Column(String(100), nullable=False, default="default")
    protocol = Column(String(50), nullable=False, comment="websocket-terminal | spice-graphics")
    
    # User context
    user_id = Column(String(200), nullable=False, index=True, comment="User ID from auth provider")
    user_email = Column(String(200), nullable=False, index=True)
    user_role = Column(String(100), nullable=False, default="unknown")
    
    # Session metrics (populated on disconnect events)
    duration_seconds = Column(Integer, nullable=True, comment="Total session duration in seconds")
    download_bytes = Column(Integer, nullable=True, comment="Total bytes downloaded by user")
    upload_bytes = Column(Integer, nullable=True, comment="Total bytes uploaded by user")
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        Index("ix_session_audit_created", "created_at"),
        Index("ix_session_audit_instance_host", "instance_name", "host"),
    )

    def __repr__(self) -> str:
        return f"<SessionAuditLog(session={self.session_id}, event={self.event_type}, user={self.user_email})>"


# ============================================
# Resource Metrics History (Time-Series)
# ============================================

class MetricsSnapshot(Base):
    """
    Persistent time-series storage for resource metrics.

    One row per collection cycle (~30 s). The background MetricsCollector
    inserts snapshots continuously so historical charts survive backend
    restarts and have no gaps when users are not viewing the dashboard.

    Retention is managed by a periodic cleanup job (default 7 days).
    Per-node breakdown is stored as JSONB for flexible schema evolution.
    """
    __tablename__ = "metrics_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # aggregate cluster metrics
    cpu_usage_percent = Column(
        "cpu_usage_percent", 
        Integer,  # stored as basis points (0.25% -> 25) for compact storage
        nullable=False, default=0,
        comment="CPU usage in basis points (value / 100 = percent)",
    )
    memory_usage_percent = Column(
        "memory_usage_percent",
        Integer,
        nullable=False, default=0,
        comment="Memory usage in basis points",
    )
    storage_usage_percent = Column(
        "storage_usage_percent",
        Integer,
        nullable=False, default=0,
        comment="Storage usage in basis points",
    )
    network_rx_bytes = Column(BigInteger, nullable=False, default=0)
    network_tx_bytes = Column(BigInteger, nullable=False, default=0)
    disk_io_bytes = Column(BigInteger, nullable=False, default=0)
    running_instances = Column(Integer, nullable=False, default=0)

    # per-node breakdown (JSONB for flexible schema)
    nodes = Column(JSONB, nullable=True, comment="Per-node metrics breakdown")

    # scope key - 'all' for global, org hash for tenant-specific
    scope = Column(
        String(50), nullable=False, default="all", index=True,
        comment="Scope key: 'all' for global metrics",
    )

    collected_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        comment="When the metrics were collected",
    )

    __table_args__ = (
        # primary query path: recent snapshots for a scope, ordered by time
        Index("ix_metrics_snap_scope_time", "scope", "collected_at"),
        # retention cleanup: delete old rows efficiently
        Index("ix_metrics_snap_collected", "collected_at"),
    )

