"""Durable jobs for instance creation and specification changes.

Revision ID: 025_instance_operation_jobs
Revises: 024_task_journal
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "025_instance_operation_jobs"
down_revision = "024_task_journal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "instance_operation_jobs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("cluster_id", sa.String(length=255), nullable=False),
        sa.Column("project", sa.String(length=255), server_default="default", nullable=False),
        sa.Column("instance_name", sa.String(length=255), nullable=False),
        sa.Column("instance_type", sa.String(length=32), nullable=True),
        sa.Column("requester", sa.String(length=255), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("plan_revision", sa.String(length=64), nullable=True),
        sa.Column("apply_mode", sa.String(length=24), nullable=True),
        sa.Column(
            "requested",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("before_spec", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("target_spec", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("verified_spec", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("phase", sa.String(length=24), server_default="pending", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("operation_id", sa.String(length=255), nullable=True),
        sa.Column("original_status", sa.String(length=32), nullable=True),
        sa.Column("stopped_by_job", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("locked_by", sa.String(length=64), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('create', 'specification')", name="ck_instance_operation_kind"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'success', 'failure', 'partial', 'unknown')",
            name="ck_instance_operation_status",
        ),
        sa.CheckConstraint(
            "apply_mode IS NULL OR apply_mode IN ('live_only', 'graceful_restart')",
            name="ck_instance_operation_apply_mode",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "requester", "idempotency_key", name="uq_instance_operation_request_key"
        ),
    )
    op.create_index(
        "ix_instance_operation_jobs_job_id",
        "instance_operation_jobs",
        ["job_id"],
        unique=True,
    )
    op.create_index(
        "ix_instance_operation_target_created",
        "instance_operation_jobs",
        ["cluster_id", "project", "instance_name", "created_at"],
    )
    op.create_index(
        "uq_instance_operation_active_target",
        "instance_operation_jobs",
        ["cluster_id", "project", "instance_name"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )
    op.create_index(
        "ix_instance_operation_status_updated",
        "instance_operation_jobs",
        ["status", "updated_at"],
    )
    op.create_index(
        "ix_instance_operation_lease", "instance_operation_jobs", ["locked_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_instance_operation_lease", table_name="instance_operation_jobs")
    op.drop_index("ix_instance_operation_status_updated", table_name="instance_operation_jobs")
    op.drop_index("uq_instance_operation_active_target", table_name="instance_operation_jobs")
    op.drop_index("ix_instance_operation_target_created", table_name="instance_operation_jobs")
    op.drop_index("ix_instance_operation_jobs_job_id", table_name="instance_operation_jobs")
    op.drop_table("instance_operation_jobs")
