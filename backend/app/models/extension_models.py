"""Add-on registry and per-organization installations.

An add-on is an external application. It runs on its author's own infrastructure and reaches this
deployment over the REST API and the MCP endpoint with a credential issued when it is installed.
No add-on code runs in this process and none runs in the dashboard's browser origin, so nothing
here describes a bundle, an entry point or a sandbox. What it describes is a grant.

Two tables, and the split matters. `extensions` is what an author publishes once. Installations
are what an operator consents to, one row per organization, and they are the only thing that
authorizes anything.

important: the effective reach of an installation is never read from this table alone. It is
recomputed as the intersection of what the manifest asked for, what the installing partner's own
cluster scope holds, and which clusters the organization is still bound to. Storing a flattened
grant and trusting it later is how an installation keeps working after the partner loses the
cluster, so `granted_*` here is a record of what was consented to, not a licence.

note: delivery endpoints deliberately do not live on the installation. A manifest may declare a
default endpoint and event set, and that pre-fills the form, but the subscription table is the
single authority for where anything is sent. Two places holding the same URL is how one of them
goes stale.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from app.core.database import Base

EXTENSION_STATUSES = ("draft", "published", "deprecated", "disabled")
INSTALL_STATUSES = ("active", "suspended", "revoked")


class Extension(Base):
    """An add-on definition, owned by the partner who published it.

    The manifest is submitted as JSON rather than fetched from a URL the author controls. A
    fetched manifest would be a second attacker-chosen server-side request in a codebase whose
    own SSRF module documents the limits of what it can close, and it would make the consent
    record meaningless, because the document could change between what an operator was shown and
    what is enforced. Submitted JSON is immutable by construction: the bytes validated are the
    bytes stored, and `manifest_digest` is what a consent is taken against.
    """

    __tablename__ = "extensions"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # important: immutable after create. The slug is the add-on's public identity, it appears in
    # issued credentials and audit records, and letting it move would silently re-point every
    # reference that ever named it.
    slug = Column(
        String(64), nullable=False, unique=True, index=True,
        comment="public identity, lowercase ASCII, immutable after create",
    )
    name = Column(String(100), nullable=False)
    publisher = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)

    # Rendered in the consent screen and never fetched by this server, which is why they are
    # validated without a DNS lookup. Only the webhook endpoint is stored-then-contacted.
    homepage_url = Column(String(512), nullable=True)
    support_url = Column(String(512), nullable=True)

    version = Column(String(32), nullable=False, comment="semver, consent is taken per version")
    status = Column(
        String(20), nullable=False, server_default="draft", index=True,
        comment=" | ".join(EXTENSION_STATUSES),
    )

    # The authoritative owner. An organization is attribution only.
    owner_email = Column(String(255), nullable=False, index=True)

    # important: nullable with SET NULL, deviating from the CASCADE every other organization
    # child uses. A partner may hold no organization yet, which is the same reason
    # OrganizationCluster.organization_id is nullable. CASCADE would be worse than wrong here:
    # deleting the publisher's organization would drop the definition while installations in
    # OTHER organizations survive, and the constraint cannot see those.
    publisher_organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True,
        comment="attribution only, never an authorization input",
    )

    requested_capabilities = Column(
        ARRAY(String), nullable=False,
        comment="from the manifest; the ceiling an install may consent to, never the grant",
    )
    requested_scope = Column(
        JSONB, nullable=False,
        comment="shape the manifest asks for: permission level, clusters, projects",
    )

    # A closed vocabulary of field types the dashboard renders itself. The add-on never ships UI,
    # so a schema it cannot describe is a form we cannot be made to draw.
    settings_schema = Column(
        JSONB, nullable=True, comment="declared configuration fields, rendered by the dashboard"
    )

    # Manifest defaults that pre-fill the subscription form. Not an authority for delivery.
    default_webhook_url = Column(String(512), nullable=True)
    default_webhook_events = Column(ARRAY(String), nullable=True)

    manifest = Column(JSONB, nullable=False, comment="the exact validated document")
    manifest_digest = Column(
        String(64), nullable=False, comment="sha256 of the canonical manifest; consent binds to it"
    )

    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'published', 'deprecated', 'disabled')",
            name="ck_extension_status",
        ),
        CheckConstraint(
            "cardinality(requested_capabilities) > 0",
            name="ck_extension_requests_something",
        ),
        Index("ix_extensions_owner_status", "owner_email", "status"),
    )


class ExtensionInstallation(Base):
    """One add-on bound to one organization, and the credential that came with it.

    important: `cardinality(granted_capabilities) > 0` is hard rule two written into the database
    rather than into a validator. An API key whose `capabilities` field is absent reads as
    unrestricted, which is how the pre-capability keys still in Vault work, and an installation
    must never be able to mint one of those. A Pydantic check can be bypassed by a second writer;
    a row that cannot exist without a capability cannot authorize a credential without one.
    """

    __tablename__ = "extension_installations"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # important: RESTRICT on both, deviating from the CASCADE that OrganizationMember,
    # OrganizationCluster, OrganizationPreferences and DesktopControlPolicy all use. Those are
    # pure Postgres state. This is the first organization child that owns Vault credential
    # material, and a CASCADE would delete the row while leaving a live key pair authenticating
    # against this deployment with nothing left to describe it or find it. RESTRICT forces the
    # teardown to run first, which is the behaviour wanted anyway.
    extension_id = Column(
        Integer, ForeignKey("extensions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    status = Column(
        String(20), nullable=False, server_default="active", index=True,
        comment=" | ".join(INSTALL_STATUSES),
    )

    granted_capabilities = Column(
        ARRAY(String), nullable=False, comment="what the operator consented to, never null or empty"
    )
    granted_scope = Column(
        JSONB, nullable=False,
        comment="verbatim ClusterPermission shape; re-intersected at use, not trusted as stored",
    )

    # Non-secret configuration values. Secret fields never appear here.
    settings = Column(JSONB, nullable=True, comment="values for the manifest's non-secret fields")
    # important: the names of the secret fields that have a value, not the values. A read returns
    # this list so the form can show a field as configured without the server ever handing back
    # what was typed. The values themselves live beside the credential in Vault.
    configured_secrets = Column(
        ARRAY(String), nullable=True, comment="which secret fields are set; never their contents"
    )

    api_key_id = Column(
        String(255), nullable=False, unique=True, index=True,
        comment="the live credential; free-text because the key itself lives in Vault",
    )
    # Rotation mints a new key and retires the old one by shortening its expiry, so the auth path
    # refuses it with no new enforcement code. These two columns are the grace window.
    previous_api_key_id = Column(String(255), nullable=True)
    previous_key_expires_at = Column(DateTime(timezone=True), nullable=True)
    credential_generation = Column(Integer, nullable=False, server_default="1")

    # What the operator was actually shown, so a manifest edited afterwards is detectable.
    consent_version = Column(String(32), nullable=False)
    consent_manifest_digest = Column(String(64), nullable=False)
    consented_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    installed_by = Column(String(255), nullable=False, index=True)

    suspended_at = Column(DateTime(timezone=True), nullable=True)
    suspended_by = Column(String(255), nullable=True)
    suspend_reason = Column(Text, nullable=True)
    last_scope_change_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        # A second install into the same organization would silently mint a second credential
        # nobody asked for, so it is a 409 naming the existing one instead.
        UniqueConstraint("extension_id", "organization_id", name="uq_extension_install"),
        CheckConstraint(
            "cardinality(granted_capabilities) > 0", name="ck_ext_install_caps_nonempty"
        ),
        CheckConstraint(
            "jsonb_array_length(granted_scope) > 0", name="ck_ext_install_scope_nonempty"
        ),
        CheckConstraint(
            "status IN ('active', 'suspended', 'revoked')", name="ck_ext_install_status"
        ),
        Index("ix_ext_install_status_org", "status", "organization_id"),
        # Partial: only rows mid-rotation carry one, and the sweep that clears them scans on it.
        Index(
            "ix_ext_install_prev_key",
            "previous_api_key_id",
            postgresql_where=Column("previous_api_key_id").isnot(None),
        ),
    )
