"""Forget the tag assignments and snapshot schedules of clusters that were deregistered.

The delete route forgets these rows (services/cluster_records.forget_cluster_records); this
revision sweeps the ones earlier deregistrations left. Kept, they make the scheduler retry
schedules for clusters that are gone, and a cluster registered again under the same id shows the
old tags on new instances that reuse a name.

Only a cluster id whose every registration has ended counts as deregistered. An id with an active
registration, or one that never had a registration row, keeps its rows. Downgrade restores nothing.

Revision ID: 046_forget_deregistered_records
Revises: 045_branding_inheritance
"""

from alembic import op
import sqlalchemy as sa

revision = "046_forget_deregistered_records"
down_revision = "045_branding_inheritance"
branch_labels = None
depends_on = None

_DEREGISTERED = (
    "SELECT cluster_id FROM cluster_registrations "
    "WHERE cluster_id NOT IN (SELECT cluster_id FROM cluster_registrations WHERE ended_at IS NULL)"
)

FORGET = (
    f"DELETE FROM tag_assignments WHERE resource_host IN ({_DEREGISTERED})",
    f"DELETE FROM snapshot_schedules WHERE cluster_id IN ({_DEREGISTERED})",
)


def upgrade() -> None:
    for statement in FORGET:
        op.execute(sa.text(statement))


def downgrade() -> None:
    # Nothing to restore: the rows named clusters whose registration had ended.
    pass
