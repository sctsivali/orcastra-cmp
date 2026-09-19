"""The task journal behind the bottom panel: one row per task, one per log line, one per
followed cluster.

The journal is written with raw SQL by two background writers (the events follower and the
activity sink) and read through the ORM here, so every default a writer relies on is a server
default in the migration, and the enum-shaped columns are CHECK constraints rather than Python
enums: a row the follower writes must satisfy the same rule a row the sink writes does.

`metadata` is a reserved attribute on a declarative Base, so the JSONB column keeps its SQL name
and is reached as `metadata_` from Python.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.core.database import Base

TASK_KINDS = ("lxd_operation", "dashboard_action", "upload", "rebuild", "schedule")
TASK_STATUSES = ("pending", "running", "success", "failure", "cancelled", "lost")
TASK_TERMINAL_STATUSES = ("success", "failure", "cancelled", "lost")
ACTOR_KINDS = ("user", "api_key", "system", "external")
TASK_SOURCES = ("dashboard", "events", "cli", "mcp", "integration", "schedule")
LOG_KINDS = ("audit", "lifecycle", "warning", "task")
LOG_SEVERITIES = ("info", "notice", "warning", "error")
FOLLOWER_STATES = ("connected", "disconnected", "unregistered")


def _sql_in(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


class TaskJournal(Base):
    """One task: an LXD operation, a dashboard action, an upload, a rebuild or a schedule run."""

    __tablename__ = "task_journal"

    id = Column(UUID(as_uuid=False), primary_key=True, server_default=text("gen_random_uuid()"))
    seq = Column(BigInteger, nullable=False, server_default=text("nextval('task_journal_seq')"))
    kind = Column(String(24), nullable=False)
    cluster_id = Column(String(255), nullable=False)
    project = Column(String(255), nullable=False, server_default="default")
    resource_type = Column(String(32), nullable=True)
    resource_name = Column(String(255), nullable=True)
    operation_id = Column(String(64), nullable=True)
    operation_class = Column(String(16), nullable=True)
    action = Column(String(96), nullable=True)
    description = Column(String(255), nullable=False, server_default="")
    status = Column(String(16), nullable=False)
    status_code = Column(Integer, nullable=True)
    progress = Column(String(255), nullable=True)
    error = Column(Text, nullable=True)
    may_cancel = Column(Boolean, nullable=False, server_default=text("false"))
    actor_email = Column(String(255), nullable=True)
    actor_role = Column(String(32), nullable=True)
    actor_kind = Column(String(16), nullable=False, server_default="system")
    api_key_id = Column(String(255), nullable=True)
    source = Column(String(16), nullable=False, server_default="events")
    request_id = Column(String(64), nullable=True)
    target_organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True
    )
    target_organization_slug = Column(String(100), nullable=True)
    metadata_ = Column("metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    lxd_created_at = Column(DateTime(timezone=True), nullable=True)
    lxd_updated_at = Column(DateTime(timezone=True), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint(f"kind IN ({_sql_in(TASK_KINDS)})", name="ck_task_journal_kind"),
        CheckConstraint(f"status IN ({_sql_in(TASK_STATUSES)})", name="ck_task_journal_status"),
        CheckConstraint(
            f"actor_kind IN ({_sql_in(ACTOR_KINDS)})", name="ck_task_journal_actor_kind"
        ),
        CheckConstraint(f"source IN ({_sql_in(TASK_SOURCES)})", name="ck_task_journal_source"),
        CheckConstraint(
            "operation_id IS NULL OR operation_id <> ''", name="ck_task_journal_op_nonblank"
        ),
        Index(
            "uq_task_journal_operation",
            "cluster_id",
            "operation_id",
            unique=True,
            postgresql_where=text("operation_id IS NOT NULL"),
        ),
        Index("ix_task_journal_cluster_updated", "cluster_id", text("updated_at DESC")),
        Index("ix_task_journal_updated", "updated_at"),
        Index("ix_task_journal_status_updated", "status", "updated_at"),
        Index("ix_task_journal_actor_created", "actor_email", "created_at"),
        Index("ix_task_journal_created", "created_at"),
        Index("ix_task_journal_seq", "seq"),
        Index(
            "ix_task_journal_correlation",
            "cluster_id",
            "project",
            "resource_type",
            "resource_name",
            "action",
            text("created_at DESC"),
        ),
        Index(
            "ix_task_journal_open",
            "cluster_id",
            "created_at",
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
    )


class ClusterLog(Base):
    """One log line: a dashboard audit record, an LXD lifecycle event, an LXD warning, or a
    note the journal wrote about itself."""

    __tablename__ = "cluster_log"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ts = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    kind = Column(String(16), nullable=False)
    severity = Column(String(16), nullable=False)
    cluster_id = Column(String(255), nullable=True)
    project = Column(String(255), nullable=True)
    resource_type = Column(String(32), nullable=True)
    resource_name = Column(String(255), nullable=True)
    action = Column(String(96), nullable=False)
    message = Column(String(512), nullable=False)
    actor_email = Column(String(255), nullable=True)
    actor_role = Column(String(32), nullable=True)
    actor_kind = Column(String(16), nullable=False, server_default="system")
    api_key_id = Column(String(255), nullable=True)
    result = Column(String(16), nullable=True)
    request_id = Column(String(64), nullable=True)
    event_id = Column(String(128), nullable=True)
    task_id = Column(
        UUID(as_uuid=False), ForeignKey("task_journal.id", ondelete="SET NULL"), nullable=True
    )
    target_organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True
    )
    details = Column(JSONB, nullable=True)

    __table_args__ = (
        CheckConstraint(f"kind IN ({_sql_in(LOG_KINDS)})", name="ck_cluster_log_kind"),
        CheckConstraint(
            f"severity IN ({_sql_in(LOG_SEVERITIES)})", name="ck_cluster_log_severity"
        ),
        Index("ix_cluster_log_cluster_ts", "cluster_id", text("ts DESC")),
        Index("ix_cluster_log_ts", text("ts DESC")),
        Index("ix_cluster_log_actor_ts", "actor_email", "ts"),
        Index("ix_cluster_log_task", "task_id"),
        Index("ix_cluster_log_kind_ts", "kind", text("ts DESC")),
        Index(
            "uq_cluster_log_event_id",
            "event_id",
            unique=True,
            postgresql_where=text("event_id IS NOT NULL"),
        ),
    )


class TaskFollowerCluster(Base):
    """The follower's view of one registered cluster's upstream event stream, kept in the
    database so every worker can answer "is this cluster's feed live" without asking the one
    that holds the socket."""

    __tablename__ = "task_follower_clusters"

    cluster_id = Column(String(255), primary_key=True)
    state = Column(String(16), nullable=False)
    since = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    last_frame_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    seeded_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint(
            f"state IN ({_sql_in(FOLLOWER_STATES)})", name="ck_task_follower_clusters_state"
        ),
    )
