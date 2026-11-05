"""Forget the tag assignments and snapshot schedules of clusters that were deregistered.

Deleting a cluster registration did not remove the tag assignments and snapshot schedules naming
it, so the scheduler kept trying schedules for clusters that no longer exist, and a cluster
registered again under the same id would show the old tags on new instances reusing a name. The
delete route now forgets them (services/cluster_records.forget_cluster_records); this revision
sweeps what earlier deregistrations left (owner decision 2026-11-05).

Only a cluster id whose every registration has ended counts as deregistered. An id with an active
registration, or one that never had a registration row, keeps its rows.

Downgrade restores nothing: the rows named clusters that no longer exist.

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
    # note: nothing to restore; the rows named clusters whose registration had ended.
    pass
