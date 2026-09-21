"""Keep unknown instance operations exclusive until reconciled.

Revision ID: 026_unknown_instance_jobs_are_active
Revises: 025_instance_operation_jobs
"""

from alembic import op
import sqlalchemy as sa


revision = "026_unknown_instance_jobs_are_active"
down_revision = "025_instance_operation_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("uq_instance_operation_active_target", table_name="instance_operation_jobs")
    op.create_index(
        "uq_instance_operation_active_target",
        "instance_operation_jobs",
        ["cluster_id", "project", "instance_name"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running', 'unknown')"),
    )


def downgrade() -> None:
    op.drop_index("uq_instance_operation_active_target", table_name="instance_operation_jobs")
    op.create_index(
        "uq_instance_operation_active_target",
        "instance_operation_jobs",
        ["cluster_id", "project", "instance_name"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )
