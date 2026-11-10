"""A recurring operator snapshot, owned by an organization rather than by a session.

The durable grant is an organization and an identifiable operator. Roles reach this process only
in a JWT, so a background pass cannot replay one. On every fire it asks Postgres whether the
organization is active and still holds the cluster and project, and whether `owner_email` created
the organization or still belongs to it. Historical `registered_by` metadata does not count as
current affiliation. The scheduler never supplies authority of its own.

`next_run_at` is when to try, and backoff moves it. `due_slot` names the snapshot, and only a
finished slot moves it. The name derives from the slot, so LXD refusing a name it already holds is
what keeps a lost row update from producing a second snapshot.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from app.core.database import Base

from app.services.snapshots.limits import (  # noqa: F401 - re-exported for the migration
    MAX_SCHEDULE_KEEP_LAST,
    MAX_SCHEDULE_LABEL,
    SCHEDULE_FREQUENCIES,
    SCHEDULE_RUN_STATES,
    SCHEDULE_STATES,
)

class SnapshotSchedule(Base):
    """One schedule per instance, enforced by the unique constraint.

    Two schedules on one instance would each reserve `keep_last` against the per-instance cap of
    five, leaving no room for a manual snapshot. The database enforces it because a read-then-write
    check races across workers.
    """

    __tablename__ = "snapshot_schedules"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    cluster_id = Column(String(255), nullable=False, index=True)
    project = Column(String(255), nullable=False, server_default="default")
    instance_name = Column(String(255), nullable=False)

    # important: an instance name is unique per project but not a stable identity. One deleted and
    # recreated under the same name may belong to another tenant on a shared cluster. Pinned at
    # creation and compared at every fire.
    instance_uuid = Column(String(64), nullable=True)

    # important: `cluster_id` is free text with no foreign key, so a cluster deleted and registered
    # again by another organization matches it exactly. The fingerprint tells the two apart.
    cluster_fingerprint = Column(String(128), nullable=True)

    label = Column(String(MAX_SCHEDULE_LABEL), nullable=False)

    frequency = Column(
        String(24), nullable=False, comment=" | ".join(SCHEDULE_FREQUENCIES)
    )
    hour_utc = Column(Integer, nullable=False)
    minute_utc = Column(Integer, nullable=False, server_default="0")
    weekday = Column(Integer, nullable=True, comment="0 Monday .. 6 Sunday, weekly only")

    keep_last = Column(Integer, nullable=False, server_default="3")
    expiry_days = Column(Integer, nullable=False, server_default="7")

    owner_email = Column(
        String(255),
        nullable=False,
        index=True,
        comment="attribution and durable actor affiliation",
    )
    # important: SET NULL, never CASCADE. Deleting an organization makes this schedule fail
    # re-authorization and pause visibly, and keeps the record that it was writing snapshots onto
    # a live cluster.
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True
    )

    state = Column(
        String(16), nullable=False, server_default="active", comment=" | ".join(SCHEDULE_STATES)
    )
    paused_reason = Column(Text, nullable=True)

    next_run_at = Column(DateTime(timezone=True), nullable=False, comment="when to try")
    due_slot = Column(DateTime(timezone=True), nullable=True, comment="what to call it")

    run_state = Column(
        String(16), nullable=False, server_default="idle", comment=" | ".join(SCHEDULE_RUN_STATES)
    )
    locked_by = Column(String(64), nullable=True)
    locked_at = Column(DateTime(timezone=True), nullable=True)

    last_run_at = Column(DateTime(timezone=True), nullable=True)
    last_run_slot = Column(DateTime(timezone=True), nullable=True)
    last_snapshot_name = Column(String(63), nullable=True)
    last_error = Column(Text, nullable=True)
    consecutive_failures = Column(Integer, nullable=False, server_default="0")
    missed_runs = Column(Integer, nullable=False, server_default="0")

    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "cluster_id", "project", "instance_name", name="uq_snapshot_schedule_instance"
        ),
        CheckConstraint(
            "frequency IN ('every_6_hours', 'every_12_hours', 'daily', 'weekly')",
            name="ck_snapshot_schedule_frequency",
        ),
        CheckConstraint("state IN ('active', 'paused')", name="ck_snapshot_schedule_state"),
        CheckConstraint("run_state IN ('idle', 'inflight')", name="ck_snapshot_schedule_run_state"),
        CheckConstraint("hour_utc BETWEEN 0 AND 23", name="ck_snapshot_schedule_hour"),
        CheckConstraint("minute_utc IN (0, 15, 30, 45)", name="ck_snapshot_schedule_minute"),
        CheckConstraint("keep_last BETWEEN 1 AND 3", name="ck_snapshot_schedule_keep_last"),
        CheckConstraint("expiry_days BETWEEN 1 AND 7", name="ck_snapshot_schedule_expiry"),
        # A weekday belongs only to a weekly schedule. On any other frequency the console would
        # render it and the runner would ignore it.
        CheckConstraint(
            "(frequency = 'weekly') = (weekday IS NOT NULL)",
            name="ck_snapshot_schedule_weekday_iff_weekly",
        ),
        CheckConstraint(
            "weekday IS NULL OR weekday BETWEEN 0 AND 6", name="ck_snapshot_schedule_weekday_range"
        ),
        # Matches the claim query's filter and order.
        Index("ix_snapshot_schedule_claimable", "state", "run_state", "next_run_at"),
        Index(
            "ix_snapshot_schedule_stuck",
            "locked_at",
            postgresql_where=Column("run_state") == "inflight",
        ),
        Index("ix_snapshot_schedule_instance", "cluster_id", "project", "instance_name"),
    )
