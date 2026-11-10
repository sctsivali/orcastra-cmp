"""Add-on registry and per-organization installations.

An add-on is an external application: it runs on its author's infrastructure and reaches this
deployment over the REST API and the MCP endpoint with a credential issued at install. No add-on
code runs in this process or in the dashboard's browser origin. `extensions` is what an author
publishes; an installation is what an operator consents to, one per organization, and only an
installation authorizes anything.

important: an installation's reach is recomputed at use as the intersection of what the manifest
asked for, the installing partner's own cluster scope, and the clusters the organization is still
bound to. `granted_*` records what was consented to; trusting it alone would keep an installation
working after the partner loses the cluster. Delivery endpoints live only on the subscription.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
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
SUBSCRIPTION_STATUSES = ("pending_verification", "active", "suspended", "disabled")
DELIVERY_STATES = ("pending", "inflight", "delivered", "failed", "dead", "dropped")


class Extension(Base):
    """An add-on definition, owned by the partner who published it.

    The manifest is submitted as JSON, never fetched from an author-controlled URL: a fetch would be
    another attacker-chosen server-side request, and the document could change between what an
    operator was shown and what is enforced. The bytes validated are the bytes stored, and consent
    is taken against `manifest_digest`.
    """

    __tablename__ = "extensions"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # important: immutable after create. The slug is the add-on's public identity in issued
    # credentials and audit records; changing it would silently re-point every reference to it.
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

    # important: nullable with SET NULL, unlike the CASCADE other organization children use. A
    # partner may hold no organization yet (as with OrganizationCluster.organization_id), and a
    # CASCADE would drop the definition while installations in other organizations survive.
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

    # A closed vocabulary of field types the dashboard renders itself; an add-on never ships UI.
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

    important: `cardinality(granted_capabilities) > 0` is enforced by the database, not only by a
    validator. An API key with no `capabilities` field reads as unrestricted (keys issued before
    capabilities work that way), and an installation must never mint one. A second writer can
    bypass a Pydantic check, never the constraint.
    """

    __tablename__ = "extension_installations"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # important: RESTRICT on both, unlike the CASCADE other organization children use. This row
    # owns Vault credential material, and a CASCADE would delete it while the key pair still
    # authenticates, with nothing left to find it by. RESTRICT forces the teardown to run first.
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
    # important: names of the secret fields that have a value, never the values, so the form can
    # show a field as configured without the server returning what was typed. The values live
    # beside the credential in Vault.
    configured_secrets = Column(
        ARRAY(String), nullable=True, comment="which secret fields are set; never their contents"
    )

    api_key_id = Column(
        String(255), nullable=False, unique=True, index=True,
        comment="the live credential; free-text because the key itself lives in Vault",
    )
    # Rotation mints a new key and retires the previous one by shortening its expiry, so the auth
    # path refuses it with no new enforcement code. These two columns are the grace window.
    previous_api_key_id = Column(String(255), nullable=True)
    previous_key_expires_at = Column(DateTime(timezone=True), nullable=True)
    credential_generation = Column(Integer, nullable=False, server_default="1")

    # What the operator was shown, so a later manifest edit is detectable.
    consent_version = Column(String(32), nullable=False)
    consent_manifest_digest = Column(String(64), nullable=False)
    # important: keep the document as well as its digest. Publishing a new version replaces the
    # manifest on the extension row, and an audit needs the text the organization approved.
    consented_manifest = Column(JSONB, nullable=True)
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
        # A second install into the same organization would mint an unrequested credential; the
        # service answers 409 naming the existing installation.
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


class WebhookSubscription(Base):
    """Where an installation's events are delivered, and the secret that signs them.

    important: only the subscription stores the endpoint; a manifest default only pre-fills the
    form, so a second stored copy cannot go stale and keep receiving events.

    The signing secret is per subscription and is not the API credential, so a receiver can verify
    a delivery without holding a live control-plane key.
    """

    __tablename__ = "extension_webhook_subscriptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    installation_id = Column(
        Integer,
        ForeignKey("extension_installations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    endpoint_url = Column(String(512), nullable=False)
    event_types = Column(ARRAY(String), nullable=False)

    # A fingerprint only. The secrets live in Vault beside the installation's credential.
    secret_fingerprint = Column(
        String(16), nullable=False, comment="first bytes of a hash, so an operator can tell two apart"
    )
    previous_secret_expires_at = Column(
        DateTime(timezone=True), nullable=True,
        comment="during rotation both signatures are sent, so a receiver switches at its own pace",
    )

    status = Column(
        String(24), nullable=False, server_default="pending_verification", index=True,
        comment=" | ".join(SUBSCRIPTION_STATUSES),
    )
    # A subscription goes live only once the endpoint answers a signed ping, so the sender cannot
    # be aimed at a receiver that never agreed to receive anything.
    verified_at = Column(DateTime(timezone=True), nullable=True)

    consecutive_failures = Column(Integer, nullable=False, server_default="0")
    last_success_at = Column(DateTime(timezone=True), nullable=True)
    last_failure_at = Column(DateTime(timezone=True), nullable=True)
    last_failure_reason = Column(Text, nullable=True)
    suspended_reason = Column(Text, nullable=True)

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
        CheckConstraint("cardinality(event_types) > 0", name="ck_subscription_has_events"),
        CheckConstraint(
            "status IN ('pending_verification', 'active', 'suspended', 'disabled')",
            name="ck_subscription_status",
        ),
        Index("ix_subscription_install_status", "installation_id", "status"),
    )


class WebhookDelivery(Base):
    """One delivery and its attempts, durable so an event cannot vanish unnoticed.

    important: deliver from this table, never from a detached asyncio task. CPython may collect an
    unreferenced task mid-flight, and a per-event task holds a database session across slow HTTP
    calls; a bulk action would start hundreds at once.

    Rows are claimed with FOR UPDATE SKIP LOCKED instead of through a leader: every worker pulls
    independent rows, and losing one worker only slows delivery. The leader lease fails open, so a
    Redis outage would make every worker lead and deliver each event once per worker.
    """

    __tablename__ = "extension_webhook_deliveries"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    subscription_id = Column(
        Integer,
        ForeignKey("extension_webhook_subscriptions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Stable across every attempt: the receiver's idempotency key, since delivery is at-least-once.
    event_id = Column(String(64), nullable=False)
    event_type = Column(String(128), nullable=False, index=True)
    payload = Column(JSONB, nullable=False, comment="the envelope, already field-filtered")

    state = Column(
        String(16), nullable=False, server_default="pending", comment=" | ".join(DELIVERY_STATES)
    )
    attempt = Column(Integer, nullable=False, server_default="0")
    next_attempt_at = Column(DateTime(timezone=True), nullable=False)

    locked_by = Column(String(64), nullable=True)
    locked_at = Column(DateTime(timezone=True), nullable=True)

    response_status = Column(Integer, nullable=True)
    response_ms = Column(Integer, nullable=True)
    # Kept only when the endpoint resolved to a public address, so a delivery log cannot turn a
    # blind SSRF into a returned one.
    response_excerpt = Column(Text, nullable=True)
    last_error = Column(Text, nullable=True)

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
        # One delivery per event per subscription, so a retried enqueue cannot double-send.
        UniqueConstraint("subscription_id", "event_id", name="uq_delivery_event"),
        CheckConstraint(
            "state IN ('pending', 'inflight', 'delivered', 'failed', 'dead', 'dropped')",
            name="ck_delivery_state",
        ),
        # The claim query orders on exactly this.
        Index("ix_delivery_claimable", "state", "next_attempt_at"),
        Index(
            "ix_delivery_stuck",
            "locked_at",
            postgresql_where=Column("state") == "inflight",
        ),
    )
