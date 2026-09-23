"""Remove redundant migration external-ID indexes.

The named unique constraints created in 030 already provide PostgreSQL backing indexes.

Revision ID: 032_migration_index_parity
Revises: 031_migration_resource_locks
"""

from alembic import op


revision = "032_migration_index_parity"
down_revision = "031_migration_resource_locks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_migration_resources_resource_id", table_name="migration_resources")
    op.drop_index("ix_migration_jobs_job_id", table_name="migration_jobs")
    op.drop_index("ix_migration_plans_plan_id", table_name="migration_plans")


def downgrade() -> None:
    op.create_index(
        "ix_migration_plans_plan_id", "migration_plans", ["plan_id"], unique=True
    )
    op.create_index("ix_migration_jobs_job_id", "migration_jobs", ["job_id"], unique=True)
    op.create_index(
        "ix_migration_resources_resource_id",
        "migration_resources",
        ["resource_id"],
        unique=True,
    )
