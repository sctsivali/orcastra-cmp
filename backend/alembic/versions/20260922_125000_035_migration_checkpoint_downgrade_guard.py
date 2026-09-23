"""Keep a compatibility revision after checkpoint-phase hardening.

Revision ID: 035_checkpoint_downgrade_guard
Revises: 034_migration_checkpoint_phase
"""

revision = "035_checkpoint_downgrade_guard"
down_revision = "034_migration_checkpoint_phase"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
