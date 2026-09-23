"""Add durable migration plans, jobs, resources, and event outbox.

Revision ID: 030_migration_center
Revises: 029_instance_audit_outbox
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "030_migration_center"
down_revision = "029_instance_audit_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "migration_plans",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("plan_id", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("intent", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="draft", nullable=False),
        sa.Column("requester", sa.String(length=255), nullable=False),
        sa.Column("requester_role", sa.String(length=16), nullable=False),
        sa.Column("source_organization_id", sa.Integer(), nullable=False),
        sa.Column("target_organization_id", sa.Integer(), nullable=False),
        sa.Column("source_cluster_id", sa.String(length=255), nullable=False),
        sa.Column("source_project", sa.String(length=255), server_default="default", nullable=False),
        sa.Column("source_resource_name", sa.String(length=255), nullable=True),
        sa.Column("source_member", sa.String(length=255), nullable=True),
        sa.Column("source_cluster_fingerprint", sa.String(length=128), nullable=False),
        sa.Column("target_cluster_id", sa.String(length=255), nullable=False),
        sa.Column("target_project", sa.String(length=255), server_default="default", nullable=False),
        sa.Column("target_resource_name", sa.String(length=255), nullable=True),
        sa.Column("target_member", sa.String(length=255), nullable=True),
        sa.Column("target_cluster_fingerprint", sa.String(length=128), nullable=False),
        sa.Column("source_key", sa.String(length=1024), nullable=False),
        sa.Column("target_key", sa.String(length=1024), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "request_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "preflight_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("retention_hours", sa.Integer(), server_default="24", nullable=False),
        sa.Column("checkpoint_requested", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "external_cutover_required",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("scope IN ('instance', 'project')", name="ck_migration_plan_scope"),
        sa.CheckConstraint("intent IN ('copy', 'move')", name="ck_migration_plan_intent"),
        sa.CheckConstraint(
            "status IN ('draft', 'preflighting', 'ready', 'blocked', 'expired', 'executed')",
            name="ck_migration_plan_status",
        ),
        sa.CheckConstraint(
            "requester_role IN ('admin', 'partner')",
            name="ck_migration_plan_requester_role",
        ),
        sa.CheckConstraint(
            "source_organization_id = target_organization_id",
            name="ck_migration_plan_same_organization",
        ),
        sa.CheckConstraint(
            "retention_hours BETWEEN 1 AND 168",
            name="ck_migration_plan_retention_hours",
        ),
        sa.CheckConstraint(
            "(scope = 'instance' AND source_resource_name IS NOT NULL) OR "
            "(scope = 'project' AND source_resource_name IS NULL)",
            name="ck_migration_plan_source_shape",
        ),
        sa.ForeignKeyConstraint(
            ["source_organization_id"],
            ["organizations.id"],
            name="fk_migration_plan_source_organization",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_organization_id"],
            ["organizations.id"],
            name="fk_migration_plan_target_organization",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_id", name="uq_migration_plan_plan_id"),
    )
    op.create_index("ix_migration_plans_plan_id", "migration_plans", ["plan_id"], unique=True)
    op.create_index(
        "ix_migration_plan_requester_updated", "migration_plans", ["requester", "updated_at"]
    )
    op.create_index(
        "ix_migration_plan_status_expiry", "migration_plans", ["status", "expires_at"]
    )
    op.create_index(
        "ix_migration_plan_source", "migration_plans", ["source_cluster_id", "source_project"]
    )
    op.create_index(
        "ix_migration_plan_target", "migration_plans", ["target_cluster_id", "target_project"]
    )

    op.create_table(
        "migration_jobs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("plan_id", sa.String(length=64), nullable=False),
        sa.Column("plan_revision", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("intent", sa.String(length=8), nullable=False),
        sa.Column("requester", sa.String(length=255), nullable=False),
        sa.Column("requester_role", sa.String(length=16), nullable=False),
        sa.Column("source_organization_id", sa.Integer(), nullable=False),
        sa.Column("target_organization_id", sa.Integer(), nullable=False),
        sa.Column("source_cluster_id", sa.String(length=255), nullable=False),
        sa.Column("source_project", sa.String(length=255), nullable=False),
        sa.Column("source_resource_name", sa.String(length=255), nullable=True),
        sa.Column("source_member", sa.String(length=255), nullable=True),
        sa.Column("source_cluster_fingerprint", sa.String(length=128), nullable=False),
        sa.Column("target_cluster_id", sa.String(length=255), nullable=False),
        sa.Column("target_project", sa.String(length=255), nullable=False),
        sa.Column("target_resource_name", sa.String(length=255), nullable=True),
        sa.Column("target_member", sa.String(length=255), nullable=True),
        sa.Column("target_cluster_fingerprint", sa.String(length=128), nullable=False),
        sa.Column("source_key", sa.String(length=1024), nullable=False),
        sa.Column("target_key", sa.String(length=1024), nullable=False),
        sa.Column("active_source_key", sa.String(length=1024), nullable=True),
        sa.Column("active_target_key", sa.String(length=1024), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("strategy", sa.String(length=64), nullable=False),
        sa.Column("downtime_class", sa.String(length=32), nullable=False),
        sa.Column("phase", sa.String(length=40), server_default="queued", nullable=False),
        sa.Column("status", sa.String(length=24), server_default="pending", nullable=False),
        sa.Column("retention_hours", sa.Integer(), server_default="24", nullable=False),
        sa.Column("retention_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "external_cutover_required",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("external_cutover_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "operation_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("safe_result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("locked_by", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fence_token", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cutover_approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("scope IN ('instance', 'project')", name="ck_migration_job_scope"),
        sa.CheckConstraint("intent IN ('copy', 'move')", name="ck_migration_job_intent"),
        sa.CheckConstraint(
            "requester_role IN ('admin', 'partner')",
            name="ck_migration_job_requester_role",
        ),
        sa.CheckConstraint(
            "source_organization_id = target_organization_id",
            name="ck_migration_job_same_organization",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'waiting', 'success', 'failure', "
            "'cancelled', 'unknown', 'manual_recovery')",
            name="ck_migration_job_status",
        ),
        sa.CheckConstraint(
            "phase IN ('queued', 'preparing_target', 'preparing_recovery', 'initial_sync', "
            "'cutover_pending', 'quiescing', 'final_sync', 'target_validating', "
            "'external_cutover_pending', 'cutting_over', 'verifying', 'source_retained', "
            "'cleanup_due', 'cleaning_up', 'reconciling', 'rollback_available', "
            "'rolling_back', 'rolled_back', 'cleanup_blocked', 'completed', 'cancelled', "
            "'manual_recovery', 'failed')",
            name="ck_migration_job_phase",
        ),
        sa.CheckConstraint(
            "retention_hours BETWEEN 1 AND 168", name="ck_migration_job_retention_hours"
        ),
        sa.CheckConstraint("revision >= 1", name="ck_migration_job_revision"),
        sa.CheckConstraint("fence_token >= 0", name="ck_migration_job_fence_token"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_migration_job_attempt_count"),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["migration_plans.plan_id"],
            name="fk_migration_job_plan",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_organization_id"],
            ["organizations.id"],
            name="fk_migration_job_source_organization",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_organization_id"],
            ["organizations.id"],
            name="fk_migration_job_target_organization",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", name="uq_migration_job_job_id"),
        sa.UniqueConstraint("requester", "idempotency_key", name="uq_migration_job_request_key"),
        sa.UniqueConstraint("active_source_key", name="uq_migration_job_active_source"),
        sa.UniqueConstraint("active_target_key", name="uq_migration_job_active_target"),
    )
    op.create_index("ix_migration_jobs_job_id", "migration_jobs", ["job_id"], unique=True)
    op.create_index(
        "ix_migration_job_status_updated", "migration_jobs", ["status", "updated_at"]
    )
    op.create_index(
        "ix_migration_job_phase_updated", "migration_jobs", ["phase", "updated_at"]
    )
    op.create_index("ix_migration_job_lease", "migration_jobs", ["lease_expires_at"])
    op.create_index(
        "ix_migration_job_org_created",
        "migration_jobs",
        ["source_organization_id", "created_at"],
    )
    op.create_index(
        "ix_migration_job_source", "migration_jobs", ["source_cluster_id", "source_project"]
    )
    op.create_index(
        "ix_migration_job_target", "migration_jobs", ["target_cluster_id", "target_project"]
    )

    op.create_table(
        "migration_resources",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("resource_id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("parent_resource_id", sa.String(length=64), nullable=True),
        sa.Column("resource_key", sa.String(length=512), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=24), server_default="pending", nullable=False),
        sa.Column(
            "source_ref",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "target_ref",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "dependency_keys",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "operation_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "progress",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("safe_result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("source_fingerprint", sa.String(length=128), nullable=True),
        sa.Column("target_fingerprint", sa.String(length=128), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("ordinal >= 0", name="ck_migration_resource_ordinal"),
        sa.CheckConstraint("attempts >= 0", name="ck_migration_resource_attempts"),
        sa.CheckConstraint(
            "state IN ('pending', 'blocked', 'preparing', 'syncing', 'ready', 'cutting_over', "
            "'retained', 'cleaning', 'completed', 'failed', 'unknown', 'skipped')",
            name="ck_migration_resource_state",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["migration_jobs.job_id"],
            name="fk_migration_resource_job",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("resource_id", name="uq_migration_resource_resource_id"),
        sa.UniqueConstraint("job_id", "resource_key", name="uq_migration_resource_job_key"),
    )
    op.create_index(
        "ix_migration_resources_resource_id",
        "migration_resources",
        ["resource_id"],
        unique=True,
    )
    op.create_index(
        "ix_migration_resource_job_ordinal", "migration_resources", ["job_id", "ordinal"]
    )
    op.create_index(
        "ix_migration_resource_job_state", "migration_resources", ["job_id", "state"]
    )

    op.create_table(
        "migration_audit_outbox",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "channel IN ('audit', 'webhook', 'cache', 'billing', 'notification')",
            name="ck_migration_outbox_channel",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'delivering', 'delivered', 'failed')",
            name="ck_migration_outbox_status",
        ),
        sa.CheckConstraint("sequence >= 0", name="ck_migration_outbox_sequence"),
        sa.CheckConstraint("attempts >= 0", name="ck_migration_outbox_attempts"),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["migration_jobs.job_id"],
            name="fk_migration_outbox_job",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", name="uq_migration_outbox_event_id"),
        sa.UniqueConstraint(
            "job_id", "channel", "sequence", name="uq_migration_outbox_job_channel_sequence"
        ),
    )
    op.create_index(
        "ix_migration_outbox_claimable",
        "migration_audit_outbox",
        ["status", "next_attempt_at", "lease_expires_at"],
    )
    op.create_index(
        "ix_migration_outbox_job_created", "migration_audit_outbox", ["job_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_migration_outbox_job_created", table_name="migration_audit_outbox")
    op.drop_index("ix_migration_outbox_claimable", table_name="migration_audit_outbox")
    op.drop_table("migration_audit_outbox")
    op.drop_index("ix_migration_resource_job_state", table_name="migration_resources")
    op.drop_index("ix_migration_resource_job_ordinal", table_name="migration_resources")
    op.drop_index("ix_migration_resources_resource_id", table_name="migration_resources")
    op.drop_table("migration_resources")
    op.drop_index("ix_migration_job_target", table_name="migration_jobs")
    op.drop_index("ix_migration_job_source", table_name="migration_jobs")
    op.drop_index("ix_migration_job_org_created", table_name="migration_jobs")
    op.drop_index("ix_migration_job_lease", table_name="migration_jobs")
    op.drop_index("ix_migration_job_phase_updated", table_name="migration_jobs")
    op.drop_index("ix_migration_job_status_updated", table_name="migration_jobs")
    op.drop_index("ix_migration_jobs_job_id", table_name="migration_jobs")
    op.drop_table("migration_jobs")
    op.drop_index("ix_migration_plan_target", table_name="migration_plans")
    op.drop_index("ix_migration_plan_source", table_name="migration_plans")
    op.drop_index("ix_migration_plan_status_expiry", table_name="migration_plans")
    op.drop_index("ix_migration_plan_requester_updated", table_name="migration_plans")
    op.drop_index("ix_migration_plans_plan_id", table_name="migration_plans")
    op.drop_table("migration_plans")
