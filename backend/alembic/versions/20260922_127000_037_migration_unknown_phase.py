"""Add an explicit unknown migration phase.

Revision ID: 037_migration_unknown_phase
Revises: 036_migration_job_plan_hash
"""

from alembic import op


revision = "037_migration_unknown_phase"
down_revision = "036_migration_job_plan_hash"
branch_labels = None
depends_on = None

_PHASES_WITH_UNKNOWN = (
    "phase IN ('queued', 'preparing_target', 'preparing_recovery', 'initial_sync', "
    "'cutover_pending', 'quiescing', 'checkpointing', 'final_sync', "
    "'target_validating', 'external_cutover_pending', 'cutting_over', 'verifying', "
    "'source_retained', 'cleanup_due', 'cleaning_up', 'unknown', 'reconciling', "
    "'rollback_available', 'rolling_back', 'rolled_back', 'cleanup_blocked', "
    "'completed', 'cancelled', 'manual_recovery', 'failed')"
)
_PHASES_BEFORE_UNKNOWN = _PHASES_WITH_UNKNOWN.replace(", 'unknown'", "")


def upgrade() -> None:
    op.drop_constraint("ck_migration_job_phase", "migration_jobs", type_="check")
    op.create_check_constraint(
        "ck_migration_job_phase", "migration_jobs", _PHASES_WITH_UNKNOWN
    )


def downgrade() -> None:
    op.execute(
        "UPDATE migration_jobs SET phase = 'reconciling' WHERE phase = 'unknown'"
    )
    op.drop_constraint("ck_migration_job_phase", "migration_jobs", type_="check")
    op.create_check_constraint(
        "ck_migration_job_phase", "migration_jobs", _PHASES_BEFORE_UNKNOWN
    )
