"""Add a durable outbox for instance operation audit events.

Revision ID: 029_instance_audit_outbox
Revises: 028_instance_job_organization
"""

from alembic import op
import sqlalchemy as sa


revision = "029_instance_audit_outbox"
down_revision = "028_instance_job_organization"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "instance_operation_audit_outbox",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("stage", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "stage IN ('accepted', 'terminal')",
            name="ck_instance_operation_audit_stage",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["instance_operation_jobs.job_id"],
            name="fk_instance_operation_audit_job",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", name="uq_instance_operation_audit_event_id"),
        sa.UniqueConstraint(
            "job_id", "stage", name="uq_instance_operation_audit_job_stage"
        ),
    )
    op.create_index(
        "ix_instance_operation_audit_pending",
        "instance_operation_audit_outbox",
        ["delivered_at", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_instance_operation_audit_pending",
        table_name="instance_operation_audit_outbox",
    )
    op.drop_table("instance_operation_audit_outbox")
