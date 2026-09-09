"""A snapshot schedule this console owns, beside the one it only reads.

The four `snapshots.*` config keys on an instance are the metered product's retention mechanism and
stay read-only. This table is the other kind of schedule: created here, executed here, and never
written into the instance's config, so `externally_managed` keeps meaning what eighteen consumers
already read it to mean.

Revision ID: 023_a_schedule_orcastra_owns
Revises: 022_install_keeps_its_manifest
"""

from alembic import op
import sqlalchemy as sa

revision = "023_a_schedule_orcastra_owns"
down_revision = "022_install_keeps_its_manifest"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "snapshot_schedules",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("cluster_id", sa.String(length=255), nullable=False),
        sa.Column("project", sa.String(length=255), server_default="default", nullable=False),
        sa.Column("instance_name", sa.String(length=255), nullable=False),
        sa.Column("instance_uuid", sa.String(length=64), nullable=True),
        sa.Column("cluster_fingerprint", sa.String(length=128), nullable=True),
        sa.Column("label", sa.String(length=42), nullable=False),
        sa.Column("frequency", sa.String(length=24), nullable=False),
        sa.Column("hour_utc", sa.Integer(), nullable=False),
        sa.Column("minute_utc", sa.Integer(), server_default="0", nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=True),
        sa.Column("keep_last", sa.Integer(), server_default="3", nullable=False),
        sa.Column("expiry_days", sa.Integer(), server_default="7", nullable=False),
        sa.Column("owner_email", sa.String(length=255), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=True),
        sa.Column("state", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("paused_reason", sa.Text(), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_slot", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_state", sa.String(length=16), server_default="idle", nullable=False),
        sa.Column("locked_by", sa.String(length=64), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_slot", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_snapshot_name", sa.String(length=63), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("missed_runs", sa.Integer(), server_default="0", nullable=False),
        # important: server defaults on the timestamps, unlike the two neighbouring migrations.
        # The scheduler updates these rows with raw SQL through a claim statement, and a NOT NULL
        # column whose only default is an ORM lambda is a constraint violation the first time
        # anything other than the ORM inserts.
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # important: SET NULL, never CASCADE. Deleting an organization must make the schedule fail
        # re-authorization and pause loudly, not erase the evidence that it was still writing
        # snapshots onto a live cluster.
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "cluster_id", "project", "instance_name", name="uq_snapshot_schedule_instance"
        ),
        sa.CheckConstraint(
            "frequency IN ('every_6_hours', 'every_12_hours', 'daily', 'weekly')",
            name="ck_snapshot_schedule_frequency",
        ),
        sa.CheckConstraint("state IN ('active', 'paused')", name="ck_snapshot_schedule_state"),
        sa.CheckConstraint(
            "run_state IN ('idle', 'inflight')", name="ck_snapshot_schedule_run_state"
        ),
        sa.CheckConstraint("hour_utc BETWEEN 0 AND 23", name="ck_snapshot_schedule_hour"),
        sa.CheckConstraint("minute_utc IN (0, 15, 30, 45)", name="ck_snapshot_schedule_minute"),
        sa.CheckConstraint("keep_last BETWEEN 1 AND 3", name="ck_snapshot_schedule_keep_last"),
        sa.CheckConstraint("expiry_days BETWEEN 1 AND 7", name="ck_snapshot_schedule_expiry"),
        sa.CheckConstraint(
            "(frequency = 'weekly') = (weekday IS NOT NULL)",
            name="ck_snapshot_schedule_weekday_iff_weekly",
        ),
        sa.CheckConstraint(
            "weekday IS NULL OR weekday BETWEEN 0 AND 6",
            name="ck_snapshot_schedule_weekday_range",
        ),
    )
    op.create_index("ix_snapshot_schedules_cluster_id", "snapshot_schedules", ["cluster_id"])
    op.create_index("ix_snapshot_schedules_owner_email", "snapshot_schedules", ["owner_email"])
    op.create_index(
        "ix_snapshot_schedule_claimable",
        "snapshot_schedules",
        ["state", "run_state", "next_run_at"],
    )
    op.create_index(
        "ix_snapshot_schedule_stuck",
        "snapshot_schedules",
        ["locked_at"],
        postgresql_where=sa.text("run_state = 'inflight'"),
    )
    op.create_index(
        "ix_snapshot_schedule_instance",
        "snapshot_schedules",
        ["cluster_id", "project", "instance_name"],
    )


def downgrade() -> None:
    op.drop_index("ix_snapshot_schedule_instance", table_name="snapshot_schedules")
    op.drop_index("ix_snapshot_schedule_stuck", table_name="snapshot_schedules")
    op.drop_index("ix_snapshot_schedule_claimable", table_name="snapshot_schedules")
    op.drop_index("ix_snapshot_schedules_owner_email", table_name="snapshot_schedules")
    op.drop_index("ix_snapshot_schedules_cluster_id", table_name="snapshot_schedules")
    op.drop_table("snapshot_schedules")
