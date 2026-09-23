"""Pin the exact plan payload hash on every migration job.

Revision ID: 036_migration_job_plan_hash
Revises: 035_checkpoint_downgrade_guard
"""

from alembic import op
import sqlalchemy as sa


revision = "036_migration_job_plan_hash"
down_revision = "035_checkpoint_downgrade_guard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable only for rows written by a pre-release build. Workers reject a missing value.
    op.add_column(
        "migration_jobs",
        sa.Column("plan_payload_hash", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("migration_jobs", "plan_payload_hash")
