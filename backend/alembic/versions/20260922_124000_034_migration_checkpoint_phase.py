"""Add explicit checkpointing phase to migration jobs.

Revision ID: 034_migration_checkpoint_phase
Revises: 033_migration_actor_identity
"""

from alembic import op


revision = "034_migration_checkpoint_phase"
down_revision = "033_migration_actor_identity"
branch_labels = None
depends_on = None

_PHASES_WITH_CHECKPOINTING = (
    "phase IN ('queued', 'preparing_target', 'preparing_recovery', 'initial_sync', "
    "'cutover_pending', 'quiescing', 'checkpointing', 'final_sync', "
    "'target_validating', 'external_cutover_pending', 'cutting_over', 'verifying', "
    "'source_retained', 'cleanup_due', 'cleaning_up', 'reconciling', "
    "'rollback_available', 'rolling_back', 'rolled_back', 'cleanup_blocked', "
    "'completed', 'cancelled', 'manual_recovery', 'failed')"
)
_PHASES_BEFORE_CHECKPOINTING = _PHASES_WITH_CHECKPOINTING.replace(
    ", 'checkpointing'", ""
)


def upgrade() -> None:
    op.drop_constraint("ck_migration_job_phase", "migration_jobs", type_="check")
    op.create_check_constraint(
        "ck_migration_job_phase", "migration_jobs", _PHASES_WITH_CHECKPOINTING
    )


def downgrade() -> None:
    # important: operators must pause migration workers before downgrade. Mapping the transient
    # phase first lets the older check constraint be restored without stranding rows.
    op.execute(
        "UPDATE migration_jobs SET phase = 'quiescing' "
        "WHERE phase = 'checkpointing'"
    )
    op.drop_constraint("ck_migration_job_phase", "migration_jobs", type_="check")
    op.create_check_constraint(
        "ck_migration_job_phase", "migration_jobs", _PHASES_BEFORE_CHECKPOINTING
    )
