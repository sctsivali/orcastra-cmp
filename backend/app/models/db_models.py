"""SQLAlchemy ORM models for the Orcastra PostgreSQL database."""

from datetime import datetime, timezone
from sqlalchemy import CheckConstraint, Column, String, Text, DateTime, Index, Integer, BigInteger, Boolean, ForeignKey, UniqueConstraint, event, func, select, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.dialects.postgresql import JSONB, ARRAY
from sqlalchemy.orm import Session, backref, relationship
from app.core.database import Base


# ============================================
# Tag System Models
# ============================================

class TagCategory(Base):
    """A tag category, identified by its prefix.

    Predefined: loc_ (location), reg_ (region), vend_ (vendor), own_ (ownership),
    env_ (environment), app_ (application type), sla_ (SLA level), cost_ (cost center).
    ex; loc_jakarta, vend_canonical, env_production.
    """
    __tablename__ = "tag_categories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    prefix = Column(String(10), unique=True, nullable=False, index=True)  # loc_, vend_, etc.
    name = Column(String(50), nullable=False)  # "Location", "Vendor"
    description = Column(Text, nullable=True)
    color = Column(String(7), nullable=True, default="#6b7280")  # Hex color for UI
    icon = Column(String(50), nullable=True, default="tag")  # Lucide icon name
    sort_order = Column(Integer, default=0)
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

    tags = relationship("Tag", back_populates="category", lazy="selectin")

    def __repr__(self) -> str:
        return f"<TagCategory(prefix={self.prefix}, name={self.name})>"


class Tag(Base):
    """A tag, named {prefix}{value} (ex; loc_jakarta). A name without a known prefix is "other"."""
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

    category = relationship("TagCategory", back_populates="tags", lazy="joined")
    assignments = relationship("TagAssignment", back_populates="tag", lazy="selectin", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_tags_category_id", "category_id"),
        Index("ix_tags_created_by", "created_by"),
    )

    def __repr__(self) -> str:
        return f"<Tag(name={self.name}, category_id={self.category_id})>"


class TagAssignment(Base):
    """A tag on a node, project or instance, and who assigned it.

    Also synced to the resource's LXD `user.tags` config (tag_service).
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

    tag = relationship("Tag", back_populates="assignments", lazy="joined")

    __table_args__ = (
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
    """Which tags a user sees and filters by in the dashboard.

    Every tag is visible by default. visibility_mode 'whitelist' shows only the visible_* entries;
    'blacklist' shows everything except the hidden_* entries.
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
    """Per-user personalization settings (branding, colors, logos)."""
    __tablename__ = "user_preferences"

    # Primary key: user ID from Authentik (OIDC sub claim)
    user_id = Column(String(255), primary_key=True, index=True)
    
    # User email for reference (not used for lookup)
    user_email = Column(String(255), nullable=True)
    
    # The PersonalizationSettings object, stored schema-less.
    settings = Column(JSONB, nullable=False, default={})
    
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
        Index("ix_user_preferences_email", "user_email"),
    )

    def __repr__(self) -> str:
        return f"<UserPreferences(user_id={self.user_id}, email={self.user_email})>"


# ============================================
# Organization & RBAC Models (Three-Tier)
# ============================================

class Organization(Base):
    """The scope that clusters, members and access policies belong to.

    Partners create organizations and register clusters into them, Tenants are assigned to them to
    reach those clusters, and Admins see all of them. `source` is 'local' (created here) or
    'orcahub' (synced through an API key integration).
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

    # The organization whose branding this one shows when it has none of its own: the partner that
    # sold to it (services/branding/provider_link.py). Branding only. important: no access decision
    # reads these columns (tests/test_the_provider_link_decides_branding_only.py).
    provider_organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    # provision | migration | admin. 'admin' with no provider is an Administrator's "no provider",
    # which provisioning does not fill again.
    provider_link_source = Column(String(16), nullable=True)
    provider_linked_at = Column(DateTime(timezone=True), nullable=True)
    provider_linked_by = Column(String(255), nullable=True)

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

    __table_args__ = (
        CheckConstraint(
            "provider_organization_id IS NULL OR provider_organization_id <> id",
            name="ck_organizations_provider_not_self",
        ),
        CheckConstraint(
            "provider_link_source IS NULL OR provider_link_source IN ('provision', 'migration', 'admin')",
            name="ck_organizations_provider_link_source",
        ),
    )

    members = relationship("OrganizationMember", back_populates="organization", lazy="selectin", cascade="all, delete-orphan")
    clusters = relationship("OrganizationCluster", back_populates="organization", lazy="selectin", cascade="all, delete-orphan")
    # Never loaded through the ORM (app.services.organization_owners reads the table with queries).
    # It exists for the delete cascade and so a new organization's first owner row follows its insert.
    owner_keys = relationship(
        "OrganizationOwnerKey", back_populates="organization", lazy="raise",
        cascade="all, delete-orphan", passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<Organization(slug={self.slug}, name={self.name})>"


# How a key entered an organization's owner set (organization_owners.OWNER_ENTRY_PATHS).
OWNER_KEY_ADDED_VIA = ("backfill", "claim", "provision", "enroll", "admin", "transfer", "carry")


class OrganizationOwnerKey(Base):
    """
    One integration key in a synced organization's owner set.

    `organizations.api_key_id` is the primary owner (the key OrcaHub callbacks use) and always has a
    row here; any key with a row may sync the organization, write its members and policies and
    provision tenant access for it. Only app.services.organization_owners and the before_flush
    listener at the end of this module write this table.
    """
    __tablename__ = "organization_owner_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    api_key_id = Column(String(255), nullable=False, index=True)
    added_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    added_via = Column(String(16), nullable=False, default="claim")
    # an Administrator's email, or key:<id> for a key that enrolled another
    added_by = Column(String(255), nullable=True)
    # the owner key that vouched for an enrollment
    proof_key_id = Column(String(255), nullable=True)

    organization = relationship("Organization", back_populates="owner_keys")

    __table_args__ = (
        UniqueConstraint("organization_id", "api_key_id", name="uq_org_owner_key"),
        CheckConstraint(
            "added_via IN ('backfill', 'claim', 'provision', 'enroll', 'admin', 'transfer', 'carry')",
            name="ck_org_owner_key_added_via",
        ),
        Index("ix_org_owner_key_org_id", "organization_id"),
    )

    def __repr__(self) -> str:
        return f"<OrganizationOwnerKey(org={self.organization_id}, key={self.api_key_id}, via={self.added_via})>"


class OrganizationMember(Base):
    """
    Membership link between a user (by email) and an organization.
    The IdP role sets the global Admin/Partner/Tenant tier. The member role also identifies
    Partners who may manage their organization's access policies; ordinary members may read
    their organization but may not grant another tenant access.
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

    organization = relationship("Organization", back_populates="members")

    __table_args__ = (
        UniqueConstraint("organization_id", "user_email", name="uq_org_member_email"),
        Index("ix_org_member_org_id", "organization_id"),
        # Authentik and this table do not agree on casing, so membership is looked up on lower().
        Index("ix_org_member_email_lower", func.lower(user_email)),
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

    organization = relationship("Organization", back_populates="clusters")

    __table_args__ = (
        UniqueConstraint("organization_id", "cluster_id", name="uq_org_cluster"),
        # important: Postgres treats NULLs as distinct, so uq_org_cluster does not
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
    """An organization's own branding (logo, colors, background).

    Partners manage it and the organization's Tenants inherit it. Same shape as
    UserPreferences.settings.
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
    # Validated on write (services/branding/validation.py). Migration 045 adds
    # CHECK settings <> '{}' in Postgres, so an organization with nothing of its own has no row and
    # inherits. Not declared here because SQLite cannot render the cast.
    settings = Column(JSONB, nullable=False)
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

    # important: passive_deletes on every backref to a NOT NULL organization_id whose foreign key
    # cascades. Without it, deleting the organization makes the ORM null the column first, the
    # database refuses, and the delete answers 409 (tests/test_an_organization_with_branding_or_
    # policies_can_be_deleted.py).
    organization = relationship("Organization", backref=backref("preferences", passive_deletes=True), uselist=False)

    def __repr__(self) -> str:
        return f"<OrganizationPreferences(org={self.organization_id})>"


# ============================================
# Tenant Access Policy Models (ACL)
# ============================================

class TenantAccessPolicy(Base):
    """A named, reusable access policy for Tenant users.

    Admins and Partners create policies with rules and assign them to organization members; a
    Tenant's cluster, project and instance permissions come from its assigned policies. `source` is
    'local' or 'orcahub' (synced through the API integration).
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
    # The purchase this policy stands for (core/tenant_term.py). Past term_ended_at the policy
    # grants viewing only; past access_ends_at it grants nothing. Null means no term was given.
    term_ended_at = Column(DateTime(timezone=True), nullable=True)
    access_ends_at = Column(DateTime(timezone=True), nullable=True)
    # When the sender decided those dates; an older decision arriving later is refused.
    term_as_of = Column(DateTime(timezone=True), nullable=True)
    # The last phase the term sweeper audited, so each boundary is recorded once.
    term_phase_recorded = Column(String(16), nullable=True)

    organization = relationship("Organization", backref=backref("access_policies", passive_deletes=True))
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
    """One rule in a policy: the cluster, project and instance it covers and what it permits.

    `project_name='*'` covers every project in the cluster and `instance_name='*'` every instance
    in the project. Permissions: view, operate, console, snapshot, file_transfer.
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
    """Links a policy to an organization member (a Tenant user).

    A Tenant may hold several policies; their permissions are unioned. `expires_at` makes the
    access temporary. `source` is 'local' or 'orcahub'.
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
    """Append-only audit log of console and terminal sessions.

    One row per `connect` and per `disconnect`; the disconnect row carries the duration and byte
    totals.
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
    
    # User context. Nullable because an agent driving a desktop through an API key has no human to
    # name, and a made-up actor in the table a tenant reads to see who reached their instances
    # cannot be told from a real one. The protocol says what kind of session it was; the activity
    # index says which key.
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

    # Session metrics, filled on disconnect.
    # important: 64-bit. A console session past 2 GiB overflows a 32-bit column, and the
    # disconnect row is written fire-and-forget, so the session would silently lose its totals.
    duration_seconds = Column(BigInteger, nullable=True, comment="Total session duration in seconds")
    download_bytes = Column(BigInteger, nullable=True, comment="Total bytes downloaded by user")
    upload_bytes = Column(BigInteger, nullable=True, comment="Total bytes uploaded by user")
    
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
    """Time-boxed, justified elevation that lets a platform admin reach a tenant's console or
    terminal without standing access.

    Enforced at console and terminal session creation: an admin acting outside the clusters it
    owns must hold an active grant for the target cluster. Scope is one cluster (cluster_id is the
    LXD host key) for data minimization and purpose limitation under UU PDP and GDPR.

    A grant is live when ``revoked_at IS NULL AND expires_at > now()``. `status` is advisory
    (expired lazily on read), so clock or cron lag never widens access.
    """
    __tablename__ = "break_glass_grants"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Exactly one principal per grant, enforced by ck_bgg_one_principal below.
    # important: an API key grantee goes in api_key_id, never into admin_email as `key:<id>`;
    # every query on admin_email would then silently match the wrong kind of principal.
    admin_email = Column(String(255), nullable=True, index=True, comment="grantee who may use it")
    api_key_id = Column(
        String(255), nullable=True, index=True,
        comment="integration key this grant authorizes, when the grantee is not a person",
    )
    cluster_id = Column(String(255), nullable=False, index=True, comment="enforced scope == LXD host key")
    # The cluster's pinned server-certificate fingerprint when the grant was issued.
    # important: cluster_id is a free-text Vault key with no foreign key, so a cluster deleted and
    # re-registered under another owner matches an old grant by string. Comparing the fingerprint
    # at enforcement ends the grant with its registration: a re-registration cannot reproduce the
    # key pair.
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
        # With no principal a grant is dead data; with both, the enforcement query would match on
        # either.
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

    A key capability says what the integration may attempt; this row says whether the organization
    that owns the cluster consents at all. A partner issues the key, but the organization owns the
    screen, and screen contents leave the deployment for a third-party model (a disclosure to a
    sub-processor), so an integrator cannot decide it alone.

    important: keep this in its own table, never in OrganizationPreferences.settings. That JSONB
    blob is written by the branding editor, where one code path can overwrite another's keys and
    a saved logo could silently clear a security consent.
    """
    __tablename__ = "desktop_control_policy"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # A real foreign key with a cascade: a deleted organization takes its consent with it, so a
    # later organization reusing the id cannot inherit it.
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    # Off by default, and an absent row also means false: an organization never asked has not
    # agreed.
    enabled = Column(Boolean, nullable=False, default=False, server_default="false")
    # Whether input may be injected, beyond capturing the screen. Consent to view a screen is not
    # consent to type into it.
    allow_input = Column(Boolean, nullable=False, default=False, server_default="false")
    # Separate consents, both off by default, and neither follows from view consent: the clipboard
    # is the highest-yield exfiltration path here, and guest access lets an add-on act as root
    # inside a container.
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

    organization = relationship(
        "Organization", backref=backref("desktop_control_policy", passive_deletes=True), uselist=False
    )

    def __repr__(self) -> str:
        return (
            f"<DesktopControlPolicy(org={self.organization_id}, enabled={self.enabled}, "
            f"allow_input={self.allow_input})>"
        )


# ============================================
# Resource Metrics History (Time-Series)
# ============================================

class MetricsSnapshot(Base):
    """Time-series resource metrics, one row per collection cycle (about 30 s).

    The background MetricsCollector writes continuously, so historical charts survive restarts and
    have no gaps while the dashboard is closed. A periodic cleanup job keeps 7 days by default.
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

    Source of truth across uvicorn workers and restarts. Bytes stage on a host-mounted dir
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

    Like ImageUploadSession, but the file becomes an LXD custom storage volume of content type
    'iso' in a chosen pool, identified by (pool, volume_name). Status flow:
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
    """A dashboard-initiated instance rebuild (LXD `POST /1.0/instances/{name}/rebuild`).

    Rebuild reinstalls the root disk from a fresh source and keeps the instance's identity,
    config and attached custom volumes. The row holds the request, the pre-rebuild config and
    the progress the UI polls. Status flow:
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


# ============================================
# Instance Operation Jobs
# ============================================

class InstanceOperationJob(Base):
    """Durable orchestration record for an instance create or specification change.

    important: store only the small, typed resource projection needed to resume and verify an
    operation; raw instance configuration and cloud-init never belong here. A worker may disappear
    after stopping a guest or submitting an LXD operation, so the phase, lease and readback fields
    let another worker reconcile the daemon's state before doing anything else.
    """

    __tablename__ = "instance_operation_jobs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    job_id = Column(String(64), unique=True, nullable=False, index=True)
    kind = Column(String(24), nullable=False)
    cluster_id = Column(String(255), nullable=False)
    project = Column(String(255), nullable=False, server_default="default")
    instance_name = Column(String(255), nullable=False)
    instance_type = Column(String(32), nullable=True)
    requester = Column(String(255), nullable=False)
    target_organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
    )
    target_organization_slug = Column(String(100), nullable=True)
    target_organization_resolution = Column(
        String(16), nullable=False, default="ambiguous", server_default="ambiguous"
    )
    idempotency_key = Column(String(128), nullable=False)
    payload_hash = Column(String(64), nullable=False)
    plan_revision = Column(String(64), nullable=True)
    apply_mode = Column(String(24), nullable=True)
    requested = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    before_spec = Column(JSONB, nullable=True)
    target_spec = Column(JSONB, nullable=True)
    verified_spec = Column(JSONB, nullable=True)
    phase = Column(String(24), nullable=False, server_default="pending")
    status = Column(String(16), nullable=False, server_default="pending")
    operation_id = Column(String(255), nullable=True)
    operation_ids = Column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    original_status = Column(String(32), nullable=True)
    stopped_by_job = Column(Boolean, nullable=False, default=False, server_default="false")
    result = Column(JSONB, nullable=True)
    error = Column(Text, nullable=True)
    locked_by = Column(String(64), nullable=True)
    locked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=text("now()"),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        server_default=text("now()"),
        nullable=False,
    )
    finished_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "requester", "idempotency_key", name="uq_instance_operation_request_key"
        ),
        CheckConstraint(
            "kind IN ('create', 'specification')", name="ck_instance_operation_kind"
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'success', 'failure', 'partial', 'unknown')",
            name="ck_instance_operation_status",
        ),
        CheckConstraint(
            "apply_mode IS NULL OR apply_mode IN ('live_only', 'graceful_restart')",
            name="ck_instance_operation_apply_mode",
        ),
        CheckConstraint(
            "target_organization_resolution IN ('exact', 'ambiguous', 'unbound')",
            name="ck_instance_operation_org_resolution",
        ),
        Index(
            "ix_instance_operation_target_created",
            "cluster_id",
            "project",
            "instance_name",
            "created_at",
        ),
        Index(
            "uq_instance_operation_active_target",
            "cluster_id",
            "project",
            "instance_name",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running', 'unknown')"),
        ),
        Index("ix_instance_operation_status_updated", "status", "updated_at"),
        Index("ix_instance_operation_lease", "locked_at"),
    )


class InstanceOperationAuditOutbox(Base):
    """Durable pointer to an accepted or terminal instance-operation audit event.

    Event data is reconstructed from the safe projection in ``InstanceOperationJob``. Keeping
    only the job pointer and stage here prevents cloud-init, credentials or raw daemon config
    from being copied into another persistence surface.
    """

    __tablename__ = "instance_operation_audit_outbox"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    event_id = Column(String(64), unique=True, nullable=False)
    job_id = Column(
        String(64),
        ForeignKey("instance_operation_jobs.job_id", ondelete="CASCADE"),
        nullable=False,
    )
    stage = Column(String(16), nullable=False)
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    last_error = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=text("now()"),
        nullable=False,
    )
    delivered_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("job_id", "stage", name="uq_instance_operation_audit_job_stage"),
        CheckConstraint(
            "stage IN ('accepted', 'terminal')",
            name="ck_instance_operation_audit_stage",
        ),
        Index("ix_instance_operation_audit_pending", "delivered_at", "created_at"),
    )


@event.listens_for(Session, "before_flush")
def _keep_the_primary_in_the_owner_set(session: Session, _flush_context: object, _instances: object) -> None:
    """Whenever an organization's primary owner key is set, make sure that key has an owner row.

    The primary is always in the owner set (a deferred foreign key in PostgreSQL), so the paths that
    claim or move an organization write only api_key_id and this adds the matching row in the same
    flush. It never removes a row; that is an explicit step in app.services.organization_owners.
    `_owner_added_via` and `_owner_added_by`, set on the organization by organization_owners before
    the flush, record how the key came in; a plain claim path leaves them unset.
    """
    for obj in list(session.new) + list(session.dirty):
        if not isinstance(obj, Organization) or not obj.api_key_id:
            continue
        state = sa_inspect(obj)
        if not state.pending and not state.attrs.api_key_id.history.has_changes():
            continue
        if any(
            isinstance(row, OrganizationOwnerKey)
            and row.api_key_id == obj.api_key_id
            # __dict__, never the attribute, which could lazy-load inside the flush
            and (row.__dict__.get("organization") is obj or (obj.id is not None and row.organization_id == obj.id))
            for row in session.new
        ):
            continue
        via = getattr(obj, "_owner_added_via", None) or "claim"
        by = getattr(obj, "_owner_added_by", None)
        if state.pending:
            session.add(OrganizationOwnerKey(organization=obj, api_key_id=obj.api_key_id, added_via=via, added_by=by))
            continue
        with session.no_autoflush:
            present = session.execute(
                select(OrganizationOwnerKey.id).where(
                    OrganizationOwnerKey.organization_id == obj.id,
                    OrganizationOwnerKey.api_key_id == obj.api_key_id,
                )
            ).first()
        if present is None:
            session.add(OrganizationOwnerKey(organization_id=obj.id, api_key_id=obj.api_key_id, added_via=via, added_by=by))
