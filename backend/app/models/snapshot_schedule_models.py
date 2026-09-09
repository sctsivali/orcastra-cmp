"""A recurring operator snapshot, owned by an organization rather than by a session.

Two decisions are visible in the columns and are worth stating here, because both were taken
against a plausible alternative.

**The owner is an organization, not a person.** Roles reach this process only in a JWT and there is
no users table, so a background pass cannot learn that the operator who made a schedule has been
demoted or deactivated. What it can ask Postgres, on every fire, is whether the owning organization
still holds the cluster. Binding to the org makes that question answerable and makes the schedule
visible to colleagues who can stop it. `owner_email` is kept for attribution, never for
authorization. The residual is real and documented: deactivating an account does not stop a
schedule until the organization loses the cluster.

**`next_run_at` and `due_slot` are two columns because they answer two questions.** `next_run_at` is
when to try, and backoff moves it. `due_slot` is what the snapshot will be called, and only a
finished slot moves it. Collapsing them means a cluster that is down for six hours either retries
every tick or renames its retries, and the second silently destroys the at-most-once property: the
name is derived from the slot, and LXD refusing a name it already holds is the only thing standing
between a lost row update and a second snapshot.
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
    """One instance, one schedule. The unique constraint is load-bearing, not tidiness.

    Two schedules on one instance would each reserve `keep_last` against a per-instance cap of
    five, so the arithmetic that keeps a manual snapshot possible would become a hope. It is
    enforced in the database rather than by a read-then-write, because four workers make that race
    real.
    """

    __tablename__ = "snapshot_schedules"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    cluster_id = Column(String(255), nullable=False, index=True)
    project = Column(String(255), nullable=False, server_default="default")
    instance_name = Column(String(255), nullable=False)

    # important: LXD instance names are unique per project, not stable identities. An instance
    # deleted and recreated under the same name is a different instance, possibly a different
    # tenant's, on a shared cluster. Pinned at creation and compared at every fire.
    instance_uuid = Column(String(64), nullable=True)

    # important: `cluster_id` is free text with no foreign key anywhere in this schema, so a
    # cluster deleted and re-registered by another organization matches the old string exactly.
    # The fingerprint is what distinguishes the registration from the name.
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

    owner_email = Column(String(255), nullable=False, index=True, comment="attribution, not authority")
    # important: SET NULL, never CASCADE. Deleting an organization must make this schedule fail
    # re-authorization and pause loudly, not erase the evidence that something was still writing
    # snapshots onto a live cluster.
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
        # A weekday belongs to a weekly schedule and to nothing else. Stored against a daily one it
        # is a value the console would render and the runner would ignore, which is how a schedule
        # comes to say one thing and do another.
        CheckConstraint(
            "(frequency = 'weekly') = (weekday IS NOT NULL)",
            name="ck_snapshot_schedule_weekday_iff_weekly",
        ),
        CheckConstraint(
            "weekday IS NULL OR weekday BETWEEN 0 AND 6", name="ck_snapshot_schedule_weekday_range"
        ),
        # The claim query orders on exactly this, so the index is the query plan rather than a hope.
        Index("ix_snapshot_schedule_claimable", "state", "run_state", "next_run_at"),
        Index(
            "ix_snapshot_schedule_stuck",
            "locked_at",
            postgresql_where=Column("run_state") == "inflight",
        ),
        Index("ix_snapshot_schedule_instance", "cluster_id", "project", "instance_name"),
    )
