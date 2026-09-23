"""Add cross-direction and project-hierarchy migration locks.

Revision ID: 031_migration_resource_locks
Revises: 030_migration_center
"""

from alembic import op
import sqlalchemy as sa


revision = "031_migration_resource_locks"
down_revision = "030_migration_center"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "migration_resource_locks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("lock_key", sa.String(length=1024), nullable=False),
        sa.Column("lock_scope", sa.String(length=16), nullable=False),
        sa.Column("cluster_id", sa.String(length=255), nullable=False),
        sa.Column("project", sa.String(length=255), nullable=False),
        sa.Column("resource_name", sa.String(length=255), nullable=True),
        sa.Column(
            "acquired_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "lock_scope IN ('project', 'instance')",
            name="ck_migration_resource_lock_scope",
        ),
        sa.CheckConstraint(
            "(lock_scope = 'project' AND resource_name IS NULL) OR "
            "(lock_scope = 'instance' AND resource_name IS NOT NULL)",
            name="ck_migration_resource_lock_shape",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["migration_jobs.job_id"],
            name="fk_migration_resource_lock_job",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lock_key", name="uq_migration_resource_lock_key"),
        sa.UniqueConstraint(
            "job_id", "lock_key", name="uq_migration_resource_lock_job_key"
        ),
    )
    op.create_index(
        "ix_migration_resource_lock_job", "migration_resource_locks", ["job_id"]
    )
    op.create_index(
        "ix_migration_resource_lock_project",
        "migration_resource_locks",
        ["cluster_id", "project"],
    )


def downgrade() -> None:
    op.drop_index("ix_migration_resource_lock_project", table_name="migration_resource_locks")
    op.drop_index("ix_migration_resource_lock_job", table_name="migration_resource_locks")
    op.drop_table("migration_resource_locks")
