"""The task journal, the cluster log, and the follower's per-cluster upstream state.

LXD reaps a finished operation within seconds and each worker's events hub is per process, so
these tables are the durable record of what ran on a cluster, who started it and what it left
behind: one row per task, one per log line, one per followed cluster.

Every id and timestamp has a server default because the follower and the activity sink write
these rows with raw SQL.

Revision ID: 024_task_journal
Revises: 023_a_schedule_orcastra_owns
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "024_task_journal"
down_revision = "023_a_schedule_orcastra_owns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `seq` is bumped on every change a row goes through, so a WebSocket tailer can order and
    # dedupe deltas without trusting `updated_at`, which concurrent writers can produce out of order.
    op.execute("CREATE SEQUENCE IF NOT EXISTS task_journal_seq")

    op.create_table(
        "task_journal",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "seq",
            sa.BigInteger(),
            server_default=sa.text("nextval('task_journal_seq')"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("cluster_id", sa.String(length=255), nullable=False),
        sa.Column("project", sa.String(length=255), server_default="default", nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=True),
        sa.Column("resource_name", sa.String(length=255), nullable=True),
        sa.Column("operation_id", sa.String(length=64), nullable=True),
        sa.Column("operation_class", sa.String(length=16), nullable=True),
        sa.Column("action", sa.String(length=96), nullable=True),
        sa.Column("description", sa.String(length=255), server_default="", nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("progress", sa.String(length=255), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("may_cancel", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("actor_email", sa.String(length=255), nullable=True),
        sa.Column("actor_role", sa.String(length=32), nullable=True),
        sa.Column("actor_kind", sa.String(length=16), server_default="system", nullable=False),
        sa.Column("api_key_id", sa.String(length=255), nullable=True),
        sa.Column("source", sa.String(length=16), server_default="events", nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("target_organization_id", sa.Integer(), nullable=True),
        sa.Column("target_organization_slug", sa.String(length=100), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("lxd_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lxd_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        # SET NULL, never CASCADE: deleting an organization must not erase what ran on its clusters.
        sa.ForeignKeyConstraint(
            ["target_organization_id"], ["organizations.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "kind IN ('lxd_operation', 'dashboard_action', 'upload', 'rebuild', 'schedule')",
            name="ck_task_journal_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'success', 'failure', 'cancelled', 'lost')",
            name="ck_task_journal_status",
        ),
        sa.CheckConstraint(
            "actor_kind IN ('user', 'api_key', 'system', 'external')",
            name="ck_task_journal_actor_kind",
        ),
        sa.CheckConstraint(
            "source IN ('dashboard', 'events', 'cli', 'mcp', 'integration', 'schedule')",
            name="ck_task_journal_source",
        ),
        # A blank operation id is not a key: the partial unique index below covers only non-null
        # ids, and the writers refuse '' before any SQL.
        sa.CheckConstraint("operation_id IS NULL OR operation_id <> ''", name="ck_task_journal_op_nonblank"),
    )
    op.create_index(
        "uq_task_journal_operation",
        "task_journal",
        ["cluster_id", "operation_id"],
        unique=True,
        postgresql_where=sa.text("operation_id IS NOT NULL"),
    )
    op.create_index(
        "ix_task_journal_cluster_updated",
        "task_journal",
        ["cluster_id", sa.text("updated_at DESC")],
    )
    op.create_index("ix_task_journal_updated", "task_journal", ["updated_at"])
    op.create_index("ix_task_journal_status_updated", "task_journal", ["status", "updated_at"])
    op.create_index("ix_task_journal_actor_created", "task_journal", ["actor_email", "created_at"])
    op.create_index("ix_task_journal_created", "task_journal", ["created_at"])
    op.create_index("ix_task_journal_seq", "task_journal", ["seq"])
    op.create_index(
        "ix_task_journal_correlation",
        "task_journal",
        ["cluster_id", "project", "resource_type", "resource_name", "action", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_task_journal_open",
        "task_journal",
        ["cluster_id", "created_at"],
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )

    op.create_table(
        "cluster_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        # NULL for a dashboard record that names no cluster (identity, settings, a denied call).
        sa.Column("cluster_id", sa.String(length=255), nullable=True),
        sa.Column("project", sa.String(length=255), nullable=True),
        sa.Column("resource_type", sa.String(length=32), nullable=True),
        sa.Column("resource_name", sa.String(length=255), nullable=True),
        sa.Column("action", sa.String(length=96), nullable=False),
        sa.Column("message", sa.String(length=512), nullable=False),
        sa.Column("actor_email", sa.String(length=255), nullable=True),
        sa.Column("actor_role", sa.String(length=32), nullable=True),
        sa.Column("actor_kind", sa.String(length=16), server_default="system", nullable=False),
        sa.Column("api_key_id", sa.String(length=255), nullable=True),
        sa.Column("result", sa.String(length=16), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("event_id", sa.String(length=128), nullable=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("target_organization_id", sa.Integer(), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["task_journal.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["target_organization_id"], ["organizations.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "kind IN ('audit', 'lifecycle', 'warning', 'task')", name="ck_cluster_log_kind"
        ),
        sa.CheckConstraint(
            "severity IN ('info', 'notice', 'warning', 'error')", name="ck_cluster_log_severity"
        ),
    )
    op.create_index("ix_cluster_log_cluster_ts", "cluster_log", ["cluster_id", sa.text("ts DESC")])
    op.create_index("ix_cluster_log_ts", "cluster_log", [sa.text("ts DESC")])
    op.create_index("ix_cluster_log_actor_ts", "cluster_log", ["actor_email", "ts"])
    op.create_index("ix_cluster_log_task", "cluster_log", ["task_id"])
    op.create_index("ix_cluster_log_kind_ts", "cluster_log", ["kind", sa.text("ts DESC")])
    # Idempotent: a sink retry or a repeated warning sweep re-inserts the same event id as a no-op.
    op.create_index(
        "uq_cluster_log_event_id",
        "cluster_log",
        ["event_id"],
        unique=True,
        postgresql_where=sa.text("event_id IS NOT NULL"),
    )

    op.create_table(
        "task_follower_clusters",
        sa.Column("cluster_id", sa.String(length=255), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column(
            "since", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("last_frame_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("seeded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("cluster_id"),
        sa.CheckConstraint(
            "state IN ('connected', 'disconnected', 'unregistered')",
            name="ck_task_follower_clusters_state",
        ),
    )


def downgrade() -> None:
    op.drop_table("task_follower_clusters")
    op.drop_index("uq_cluster_log_event_id", table_name="cluster_log")
    op.drop_index("ix_cluster_log_kind_ts", table_name="cluster_log")
    op.drop_index("ix_cluster_log_task", table_name="cluster_log")
    op.drop_index("ix_cluster_log_actor_ts", table_name="cluster_log")
    op.drop_index("ix_cluster_log_ts", table_name="cluster_log")
    op.drop_index("ix_cluster_log_cluster_ts", table_name="cluster_log")
    op.drop_table("cluster_log")
    op.drop_index("ix_task_journal_open", table_name="task_journal")
    op.drop_index("ix_task_journal_correlation", table_name="task_journal")
    op.drop_index("ix_task_journal_seq", table_name="task_journal")
    op.drop_index("ix_task_journal_created", table_name="task_journal")
    op.drop_index("ix_task_journal_actor_created", table_name="task_journal")
    op.drop_index("ix_task_journal_status_updated", table_name="task_journal")
    op.drop_index("ix_task_journal_updated", table_name="task_journal")
    op.drop_index("ix_task_journal_cluster_updated", table_name="task_journal")
    op.drop_index("uq_task_journal_operation", table_name="task_journal")
    op.drop_table("task_journal")
    op.execute("DROP SEQUENCE IF EXISTS task_journal_seq")
