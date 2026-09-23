"""Durable persistence for staged instance and project migrations.

Only sanitized control-plane projections belong in these tables. Raw daemon responses,
cloud-init, migration websocket secrets, certificates, and credentials must never be stored
here. The worker reconciles those values from short-lived in-memory state and authoritative
daemon readback instead.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.core.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MigrationPlan(Base):
    """A short-lived, side-effect-free migration plan and its latest preflight result."""

    __tablename__ = "migration_plans"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    plan_id = Column(String(64), nullable=False)
    revision = Column(Integer, nullable=False, default=1, server_default="1")
    scope = Column(String(16), nullable=False)
    intent = Column(String(8), nullable=False)
    status = Column(String(16), nullable=False, default="draft", server_default="draft")
    requester = Column(String(255), nullable=False)
    requester_role = Column(String(16), nullable=False)
    actor_issuer = Column(String(2048), nullable=True)
    actor_subject = Column(String(512), nullable=True)
    actor_email = Column(String(320), nullable=True)
    actor_provider_uuid = Column(String(36), nullable=True)
    source_organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    target_organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_cluster_id = Column(String(255), nullable=False)
    source_project = Column(String(255), nullable=False, default="default", server_default="default")
    source_resource_name = Column(String(255), nullable=True)
    source_member = Column(String(255), nullable=True)
    source_cluster_fingerprint = Column(String(128), nullable=False)
    target_cluster_id = Column(String(255), nullable=False)
    target_project = Column(String(255), nullable=False, default="default", server_default="default")
    target_resource_name = Column(String(255), nullable=True)
    target_member = Column(String(255), nullable=True)
    target_cluster_fingerprint = Column(String(128), nullable=False)
    source_key = Column(String(1024), nullable=False)
    target_key = Column(String(1024), nullable=False)
    payload_hash = Column(String(64), nullable=False)
    request_payload = Column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    preflight_payload = Column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    retention_hours = Column(Integer, nullable=False, default=24, server_default="24")
    checkpoint_requested = Column(Boolean, nullable=False, default=False, server_default="false")
    external_cutover_required = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=text("now()")
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        server_default=text("now()"),
    )

    __table_args__ = (
        UniqueConstraint("plan_id", name="uq_migration_plan_plan_id"),
        CheckConstraint("scope IN ('instance', 'project')", name="ck_migration_plan_scope"),
        CheckConstraint("intent IN ('copy', 'move')", name="ck_migration_plan_intent"),
        CheckConstraint(
            "status IN ('draft', 'preflighting', 'ready', 'blocked', 'expired', 'executed')",
            name="ck_migration_plan_status",
        ),
        CheckConstraint(
            "requester_role IN ('admin', 'partner')",
            name="ck_migration_plan_requester_role",
        ),
        CheckConstraint(
            "source_organization_id = target_organization_id",
            name="ck_migration_plan_same_organization",
        ),
        CheckConstraint(
            "retention_hours BETWEEN 1 AND 168",
            name="ck_migration_plan_retention_hours",
        ),
        CheckConstraint(
            "(scope = 'instance' AND source_resource_name IS NOT NULL) OR "
            "(scope = 'project' AND source_resource_name IS NULL)",
            name="ck_migration_plan_source_shape",
        ),
        Index("ix_migration_plan_requester_updated", "requester", "updated_at"),
        Index("ix_migration_plan_status_expiry", "status", "expires_at"),
        Index("ix_migration_plan_source", "source_cluster_id", "source_project"),
        Index("ix_migration_plan_target", "target_cluster_id", "target_project"),
    )


class MigrationJob(Base):
    """Durable state machine for one accepted migration execution."""

    __tablename__ = "migration_jobs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    job_id = Column(String(64), nullable=False)
    plan_id = Column(
        String(64),
        ForeignKey("migration_plans.plan_id", ondelete="RESTRICT"),
        nullable=False,
    )
    plan_revision = Column(Integer, nullable=False)
    revision = Column(Integer, nullable=False, default=1, server_default="1")
    scope = Column(String(16), nullable=False)
    intent = Column(String(8), nullable=False)
    requester = Column(String(255), nullable=False)
    requester_role = Column(String(16), nullable=False)
    actor_issuer = Column(String(2048), nullable=True)
    actor_subject = Column(String(512), nullable=True)
    actor_email = Column(String(320), nullable=True)
    actor_provider_uuid = Column(String(36), nullable=True)
    source_organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    target_organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_cluster_id = Column(String(255), nullable=False)
    source_project = Column(String(255), nullable=False)
    source_resource_name = Column(String(255), nullable=True)
    source_member = Column(String(255), nullable=True)
    source_cluster_fingerprint = Column(String(128), nullable=False)
    target_cluster_id = Column(String(255), nullable=False)
    target_project = Column(String(255), nullable=False)
    target_resource_name = Column(String(255), nullable=True)
    target_member = Column(String(255), nullable=True)
    target_cluster_fingerprint = Column(String(128), nullable=False)
    source_key = Column(String(1024), nullable=False)
    target_key = Column(String(1024), nullable=False)
    # Nullable-key uniqueness is portable: active jobs carry the canonical key and terminal
    # jobs clear it. Databases permit multiple NULL values while rejecting two active rows.
    active_source_key = Column(String(1024), nullable=True)
    active_target_key = Column(String(1024), nullable=True)
    idempotency_key = Column(String(128), nullable=False)
    payload_hash = Column(String(64), nullable=False)
    plan_payload_hash = Column(String(64), nullable=True)
    strategy = Column(String(64), nullable=False)
    downtime_class = Column(String(32), nullable=False)
    phase = Column(String(40), nullable=False, default="queued", server_default="queued")
    status = Column(String(24), nullable=False, default="pending", server_default="pending")
    retention_hours = Column(Integer, nullable=False, default=24, server_default="24")
    retention_deadline = Column(DateTime(timezone=True), nullable=True)
    external_cutover_required = Column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    external_cutover_confirmed_at = Column(DateTime(timezone=True), nullable=True)
    operation_refs = Column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    safe_result = Column(JSONB, nullable=True)
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    locked_by = Column(String(64), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    fence_token = Column(BigInteger, nullable=False, default=0, server_default="0")
    attempt_count = Column(Integer, nullable=False, default=0, server_default="0")
    cancel_requested_at = Column(DateTime(timezone=True), nullable=True)
    cutover_approved_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=text("now()")
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        server_default=text("now()"),
    )
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("job_id", name="uq_migration_job_job_id"),
        UniqueConstraint("requester", "idempotency_key", name="uq_migration_job_request_key"),
        UniqueConstraint("active_source_key", name="uq_migration_job_active_source"),
        UniqueConstraint("active_target_key", name="uq_migration_job_active_target"),
        CheckConstraint("scope IN ('instance', 'project')", name="ck_migration_job_scope"),
        CheckConstraint("intent IN ('copy', 'move')", name="ck_migration_job_intent"),
        CheckConstraint(
            "requester_role IN ('admin', 'partner')",
            name="ck_migration_job_requester_role",
        ),
        CheckConstraint(
            "source_organization_id = target_organization_id",
            name="ck_migration_job_same_organization",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'waiting', 'success', 'failure', "
            "'cancelled', 'unknown', 'manual_recovery')",
            name="ck_migration_job_status",
        ),
        CheckConstraint(
            "phase IN ('queued', 'preparing_target', 'preparing_recovery', 'initial_sync', "
            "'cutover_pending', 'quiescing', 'final_sync', 'target_validating', "
            "'checkpointing', "
            "'external_cutover_pending', 'cutting_over', 'verifying', 'source_retained', "
            "'cleanup_due', 'cleaning_up', 'unknown', 'reconciling', 'rollback_available', "
            "'rolling_back', 'rolled_back', 'cleanup_blocked', 'completed', 'cancelled', "
            "'manual_recovery', 'failed')",
            name="ck_migration_job_phase",
        ),
        CheckConstraint(
            "retention_hours BETWEEN 1 AND 168",
            name="ck_migration_job_retention_hours",
        ),
        CheckConstraint("revision >= 1", name="ck_migration_job_revision"),
        CheckConstraint("fence_token >= 0", name="ck_migration_job_fence_token"),
        CheckConstraint("attempt_count >= 0", name="ck_migration_job_attempt_count"),
        Index("ix_migration_job_status_updated", "status", "updated_at"),
        Index("ix_migration_job_phase_updated", "phase", "updated_at"),
        Index("ix_migration_job_lease", "lease_expires_at"),
        Index("ix_migration_job_org_created", "source_organization_id", "created_at"),
        Index("ix_migration_job_source", "source_cluster_id", "source_project"),
        Index("ix_migration_job_target", "target_cluster_id", "target_project"),
    )


class MigrationResourceLock(Base):
    """Exclusive cross-direction lock for every project and instance a job touches.

    Every migration takes source and target project locks. Instance migrations additionally
    take both instance locks. The project lock is intentionally conservative: it prevents an
    instance migration from racing a whole-project relocation, even when their resource keys
    differ. Rows live until terminal reconciliation and are then deleted transactionally.
    """

    __tablename__ = "migration_resource_locks"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    job_id = Column(
        String(64),
        ForeignKey("migration_jobs.job_id", ondelete="CASCADE"),
        nullable=False,
    )
    lock_key = Column(String(1024), nullable=False)
    lock_scope = Column(String(16), nullable=False)
    cluster_id = Column(String(255), nullable=False)
    project = Column(String(255), nullable=False)
    resource_name = Column(String(255), nullable=True)
    acquired_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("lock_key", name="uq_migration_resource_lock_key"),
        UniqueConstraint("job_id", "lock_key", name="uq_migration_resource_lock_job_key"),
        CheckConstraint(
            "lock_scope IN ('project', 'instance')",
            name="ck_migration_resource_lock_scope",
        ),
        CheckConstraint(
            "(lock_scope = 'project' AND resource_name IS NULL) OR "
            "(lock_scope = 'instance' AND resource_name IS NOT NULL)",
            name="ck_migration_resource_lock_shape",
        ),
        Index("ix_migration_resource_lock_job", "job_id"),
        Index("ix_migration_resource_lock_project", "cluster_id", "project"),
    )


class MigrationResource(Base):
    """One typed node in a migration job's dependency graph."""

    __tablename__ = "migration_resources"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    resource_id = Column(String(64), nullable=False)
    job_id = Column(
        String(64),
        ForeignKey("migration_jobs.job_id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_resource_id = Column(String(64), nullable=True)
    resource_key = Column(String(512), nullable=False)
    resource_type = Column(String(64), nullable=False)
    ordinal = Column(Integer, nullable=False)
    state = Column(String(24), nullable=False, default="pending", server_default="pending")
    source_ref = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    target_ref = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    dependency_keys = Column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    operation_refs = Column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    progress = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    safe_result = Column(JSONB, nullable=True)
    source_fingerprint = Column(String(128), nullable=True)
    target_fingerprint = Column(String(128), nullable=True)
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=text("now()")
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        server_default=text("now()"),
    )
    finished_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("resource_id", name="uq_migration_resource_resource_id"),
        UniqueConstraint("job_id", "resource_key", name="uq_migration_resource_job_key"),
        CheckConstraint("ordinal >= 0", name="ck_migration_resource_ordinal"),
        CheckConstraint("attempts >= 0", name="ck_migration_resource_attempts"),
        CheckConstraint(
            "state IN ('pending', 'blocked', 'preparing', 'syncing', 'ready', 'cutting_over', "
            "'retained', 'cleaning', 'completed', 'failed', 'unknown', 'skipped')",
            name="ck_migration_resource_state",
        ),
        Index("ix_migration_resource_job_ordinal", "job_id", "ordinal"),
        Index("ix_migration_resource_job_state", "job_id", "state"),
    )


class MigrationAuditOutbox(Base):
    """Transactional, retryable pointer for sanitized migration lifecycle events."""

    __tablename__ = "migration_audit_outbox"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    event_id = Column(String(64), nullable=False)
    job_id = Column(
        String(64),
        ForeignKey("migration_jobs.job_id", ondelete="CASCADE"),
        nullable=False,
    )
    channel = Column(String(16), nullable=False)
    event_type = Column(String(64), nullable=False)
    sequence = Column(Integer, nullable=False)
    payload = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    status = Column(String(16), nullable=False, default="pending", server_default="pending")
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)
    locked_by = Column(String(64), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=text("now()")
    )
    delivered_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("event_id", name="uq_migration_outbox_event_id"),
        UniqueConstraint(
            "job_id", "channel", "sequence", name="uq_migration_outbox_job_channel_sequence"
        ),
        CheckConstraint(
            "channel IN ('audit', 'webhook', 'cache', 'billing', 'notification')",
            name="ck_migration_outbox_channel",
        ),
        CheckConstraint(
            "status IN ('pending', 'delivering', 'delivered', 'failed')",
            name="ck_migration_outbox_status",
        ),
        CheckConstraint("sequence >= 0", name="ck_migration_outbox_sequence"),
        CheckConstraint("attempts >= 0", name="ck_migration_outbox_attempts"),
        Index(
            "ix_migration_outbox_claimable",
            "status",
            "next_attempt_at",
            "lease_expires_at",
        ),
        Index("ix_migration_outbox_job_created", "job_id", "created_at"),
    )
