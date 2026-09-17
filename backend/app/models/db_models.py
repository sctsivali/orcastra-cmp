"""
Database Models for Orcastra CMP.
SQLAlchemy ORM models for PostgreSQL.
"""

from datetime import datetime, timezone
from sqlalchemy import CheckConstraint, Column, String, Text, DateTime, Index, Integer, BigInteger, Boolean, ForeignKey, UniqueConstraint, text
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
    - vend_ : Vendor (ex; vend_canonical, vend_acme)
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
      - 'local': created directly in Orcastra CMP
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
    # nullable: a cluster registered by a partner who has no org yet is owned by the
    # registrant (registered_by) until their org is synced, then reconcile binds it.
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True)
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
        # important: Postgres treats NULLs as distinct, so uq_org_cluster does NOT
        # guard org-less rows. This partial unique index keeps at most one unbound
        # (organization_id IS NULL) ownership row per cluster.
        Index(
            "uq_org_cluster_null",
            "cluster_id",
            unique=True,
            postgresql_where=text("organization_id IS NULL"),
        ),
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
      - 'local': created in Orcastra CMP
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
      - 'local': assigned via Orcastra CMP
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
    protocol = Column(String(50), nullable=False, comment="websocket-terminal | spice-graphics | mcp-desktop")
    
    # User context
    # Nullable because an agent driving a desktop through an API key has no human to name.
    # Writing one anyway would fabricate an identity into the table a tenant reads to see who
    # reached their instances, and a fabricated actor is indistinguishable from a real one
    # afterwards. The protocol says what kind of session it was; the activity index carries which
    # key it was.
    user_id = Column(String(200), nullable=True, index=True, comment="User ID from auth provider")
    user_email = Column(String(200), nullable=True, index=True)
    user_role = Column(String(100), nullable=False, default="unknown")

    # Compliance / accountability context: which tenant org owns the accessed cluster,
    # and (when the actor used break-glass) the grant that authorized this access.
    target_organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    target_org_slug = Column(String(100), nullable=True)
    break_glass_grant_id = Column(
        Integer, ForeignKey("break_glass_grants.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Session metrics (populated on disconnect events).
    # important: 64-bit. A console session past 2 GiB overflowed a 32-bit column, and
    # because the disconnect row is written fire-and-forget the failure was silent -
    # the session lost its duration and totals entirely. See migration 015.
    duration_seconds = Column(BigInteger, nullable=True, comment="Total session duration in seconds")
    download_bytes = Column(BigInteger, nullable=True, comment="Total bytes downloaded by user")
    upload_bytes = Column(BigInteger, nullable=True, comment="Total bytes uploaded by user")
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        Index("ix_session_audit_created", "created_at"),
        Index("ix_session_audit_instance_host", "instance_name", "host"),
    )

    def __repr__(self) -> str:
        return f"<SessionAuditLog(session={self.session_id}, event={self.event_type}, user={self.user_email})>"


# ============================================
# Break-Glass Grants (least-privilege admin access)
# ============================================

class BreakGlassGrant(Base):
    """
    Time-boxed, justified elevation that lets a platform admin access a tenant's
    sensitive resources (console / terminal into a VM) that they otherwise cannot
    reach without standing god-view.

    Enforced at console/terminal session creation: an admin acting outside their
    owned cluster set must hold an active grant for the target cluster. Scope is
    per-cluster (cluster_id == the LXD host key) to keep the blast radius minimal
    (data minimization / purpose limitation under UU PDP + GDPR).

    Authoritative "is this grant live?" check is
    ``revoked_at IS NULL AND expires_at > now()`` - status is advisory (lazy-expired
    on read) so a clock/cron lag never widens access.
    """
    __tablename__ = "break_glass_grants"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Exactly one principal per grant, enforced by ck_bgg_one_principal below.
    #
    # important: admin_email became nullable when API keys became grantable. A key has no human
    # behind it, so it cannot be squeezed into an email column, and encoding one as `key:<id>`
    # would make every existing query silently match the wrong kind of principal. Widening the
    # column and adding the check keeps both shapes explicit.
    admin_email = Column(String(255), nullable=True, index=True, comment="grantee who may use it")
    api_key_id = Column(
        String(255), nullable=True, index=True,
        comment="integration key this grant authorizes, when the grantee is not a person",
    )
    cluster_id = Column(String(255), nullable=False, index=True, comment="enforced scope == LXD host key")
    # The cluster's pinned server-certificate fingerprint when the grant was issued.
    #
    # important: cluster_id is a free-text Vault key with no foreign key anywhere, so a cluster
    # that is deleted and re-registered under a different owner matches an old grant by string.
    # For a console session that is a disclosure; for desktop control it is hands on a stranger's
    # keyboard. Comparing the fingerprint at enforcement time is what makes the grant die with the
    # registration it was issued against, since a re-registration cannot reproduce the key pair.
    cluster_fingerprint = Column(
        String(128), nullable=True,
        comment="pinned server cert fingerprint at issue; refuses a re-registered cluster",
    )
    # denormalized for the tenant-visible trail; nullable so an unbound cluster still grants
    target_organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True
    )
    target_org_slug = Column(String(100), nullable=True)

    reason = Column(Text, nullable=False, comment="justification (compliance requirement)")
    ticket_ref = Column(String(255), nullable=True)
    status = Column(String(20), nullable=False, default="active", comment="active | expired | revoked")

    created_by = Column(String(255), nullable=False)
    approved_by = Column(String(255), nullable=True, comment="dual-control hook (Phase 3); NULL for self-service")

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revoked_by = Column(String(255), nullable=True)

    __table_args__ = (
        # hot enforcement lookup: active grant for (admin, cluster)
        Index("ix_bgg_active_lookup", "admin_email", "cluster_id", "status", "expires_at"),
        Index("ix_bgg_key_lookup", "api_key_id", "cluster_id", "status", "expires_at"),
        Index("ix_bgg_org", "target_organization_id"),
        Index("ix_bgg_created", "created_at"),
        # A grant with neither principal authorizes nobody and would be dead data; one with both
        # would be ambiguous about who it covers, and the enforcement query would match on either.
        CheckConstraint(
            "(admin_email IS NOT NULL AND api_key_id IS NULL) "
            "OR (admin_email IS NULL AND api_key_id IS NOT NULL)",
            name="ck_bgg_one_principal",
        ),
    )

    def __repr__(self) -> str:
        principal = self.admin_email or f"key:{self.api_key_id}"
        return f"<BreakGlassGrant(grantee={principal}, cluster={self.cluster_id}, status={self.status})>"


class DesktopControlPolicy(Base):
    """Whether an organization permits an AI agent to see and drive its guests' desktops.

    A capability on a key says what the INTEGRATION may attempt. This says whether the
    organization that owns the cluster consents to it at all, and the two are different consents
    given by different people: a partner issues the key, the organization owns the screen. Screen
    contents leave the deployment for a third-party model, which is a disclosure to a
    sub-processor rather than an ordinary API call, so it is not something an integrator can
    decide alone.

    important: its own table, NOT a field in OrganizationPreferences.settings. That column is a
    personalization JSONB blob written by the branding editor, and this repository has already
    shipped bugs where one code path overwrote another's keys in it. A security consent silently
    cleared by somebody saving a logo is a defect class this project has paid for once already.
    """
    __tablename__ = "desktop_control_policy"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # A real foreign key with a cascade, unlike cluster_id elsewhere in this file: an organization
    # that is deleted takes its consent with it rather than leaving a row that a later
    # organization reusing the id would inherit.
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    # Default off. Consent is given, never assumed, and an absent row means the same as false so
    # an organization that has never been asked is not treated as having agreed.
    enabled = Column(Boolean, nullable=False, default=False, server_default="false")
    # Whether input may be injected, as opposed to only capturing the screen. Separate because
    # watching and touching are different consents: a support desk that may look at a stuck screen
    # has not thereby been allowed to type into it.
    allow_input = Column(Boolean, nullable=False, default=False, server_default="false")
    # Separate switches, because they are separate disclosures. The clipboard used to ride on
    # view consent while the code calls it the highest-yield exfiltration primitive here, and
    # guest access rode on it too, so enabling screenshots silently authorized an add-on to take
    # root inside a container. Both default to refusing, so nothing is granted by this migration.
    allow_clipboard = Column(Boolean, nullable=False, server_default="false")
    allow_guest_access = Column(Boolean, nullable=False, server_default="false")

    updated_by = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    organization = relationship("Organization", backref="desktop_control_policy", uselist=False)

    def __repr__(self) -> str:
        return (
            f"<DesktopControlPolicy(org={self.organization_id}, enabled={self.enabled}, "
            f"allow_input={self.allow_input})>"
        )


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



# ============================================
# Image Upload Session (OrcaHub custom-image file uploads)
# ============================================

class ImageUploadSession(Base):
    """Resumable upload + LXD import session for an OrcaHub partner custom image.

    Source of truth across the 4 uvicorn workers and restarts. Bytes stage on a host-mounted dir
    (staging_path); the row tracks the resumable offset and the import result. Status flow:
    pending -> uploading -> uploaded -> importing -> ready | failed.
    """
    __tablename__ = "image_upload_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(64), unique=True, nullable=False, index=True)
    cluster_id = Column(String(255), nullable=False, index=True)
    project = Column(String(255), nullable=False, server_default="default")
    alias = Column(String(255), nullable=False)
    instance_type = Column(String(32), nullable=False)  # container | virtual-machine
    image_ref = Column(String(255), nullable=True)       # OrcaHub MarketplaceImage id
    api_key_id = Column(String(255), nullable=True)
    expected_sha256 = Column(String(64), nullable=True)
    max_bytes = Column(BigInteger, nullable=False)
    total_bytes = Column(BigInteger, nullable=False)
    upload_offset = Column(BigInteger, nullable=False, server_default="0")
    status = Column(String(32), nullable=False, server_default="pending", index=True)
    fingerprint = Column(String(64), nullable=True)
    size = Column(BigInteger, nullable=True)
    error = Column(Text, nullable=True)
    staging_path = Column(String(512), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)


class IsoUploadSession(Base):
    """Resumable upload + LXD import session for an OrcaHub partner custom ISO.

    Mirrors ImageUploadSession, but the file is imported as an LXD custom storage volume of
    content-type 'iso' into a chosen pool (not the image store). Identity is (pool, volume_name)
    rather than an image fingerprint/alias. Status flow:
    pending -> uploading -> uploaded -> importing -> ready | failed.
    """
    __tablename__ = "iso_upload_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(64), unique=True, nullable=False, index=True)
    cluster_id = Column(String(255), nullable=False, index=True)
    project = Column(String(255), nullable=False, server_default="default")
    pool = Column(String(255), nullable=False)              # target storage pool
    volume_name = Column(String(255), nullable=False)       # LXD custom volume name (the ISO)
    iso_ref = Column(String(255), nullable=True)            # OrcaHub CustomIso id
    api_key_id = Column(String(255), nullable=True)
    expected_sha256 = Column(String(64), nullable=True)
    max_bytes = Column(BigInteger, nullable=False)
    total_bytes = Column(BigInteger, nullable=False)
    upload_offset = Column(BigInteger, nullable=False, server_default="0")
    status = Column(String(32), nullable=False, server_default="pending", index=True)
    size = Column(BigInteger, nullable=True)
    error = Column(Text, nullable=True)
    staging_path = Column(String(512), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)


# ============================================
# Instance Rebuild Jobs
# ============================================

class RebuildJob(Base):
    """A record of an instance 'rebuild' (LXD `POST /1.0/instances/{name}/rebuild`).

    Rebuild reinstalls the root disk from a fresh source while preserving the
    instance's identity, config, and attached custom volumes. This table is the
    durable, pollable record of one such job (dashboard-initiated), capturing what
    was requested, a snapshot of the pre-rebuild config, and its progress so the
    UI can show status/history. Status flow:
    pending -> stopping -> rebuilding -> starting -> provisioning -> ready | failed.
    """
    __tablename__ = "rebuild_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(64), unique=True, nullable=False, index=True)
    cluster_id = Column(String(255), nullable=False, index=True)   # host / cluster member
    project = Column(String(255), nullable=False, server_default="default")
    instance_name = Column(String(255), nullable=False, index=True)
    # "image" (from a base image), "empty" (blank root disk, install from ISO after),
    # or "cloud-init" (image + apply cloud-init user-data on first boot).
    mode = Column(String(32), nullable=False)
    requested_image = Column(String(512), nullable=True)   # alias/fingerprint (null for empty)
    old_image = Column(String(512), nullable=True)         # previous base image, if known
    source = Column(JSONB, nullable=True)                  # the LXD rebuild `source` payload
    previous_config = Column(JSONB, nullable=True)         # snapshot of instance config pre-rebuild
    attached_volumes = Column(JSONB, nullable=True)        # preserved custom-volume devices
    cloud_init = Column(Text, nullable=True)               # cloud-init user-data applied, if any
    requester = Column(String(255), nullable=True)         # user email/id that triggered it
    operation_id = Column(String(255), nullable=True)      # LXD operation id
    status = Column(String(32), nullable=False, server_default="pending", index=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
