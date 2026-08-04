"""Create rebuild_jobs table for instance rebuild jobs.

Durable, pollable record of a dashboard-initiated instance rebuild (LXD
`POST /1.0/instances/{name}/rebuild`): what was requested, a snapshot of the
pre-rebuild config + preserved volumes, the LXD operation id, and progress.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision = "013_rebuild_jobs"
down_revision = "012_iso_upload_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rebuild_jobs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.String(64), nullable=False),
        sa.Column("cluster_id", sa.String(255), nullable=False),
        sa.Column("project", sa.String(255), nullable=False, server_default="default"),
        sa.Column("instance_name", sa.String(255), nullable=False),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("requested_image", sa.String(512), nullable=True),
        sa.Column("old_image", sa.String(512), nullable=True),
        sa.Column("source", postgresql.JSONB, nullable=True),
        sa.Column("previous_config", postgresql.JSONB, nullable=True),
        sa.Column("attached_volumes", postgresql.JSONB, nullable=True),
        sa.Column("cloud_init", sa.Text, nullable=True),
        sa.Column("requester", sa.String(255), nullable=True),
        sa.Column("operation_id", sa.String(255), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_unique_constraint("uq_rebuild_jobs_job_id", "rebuild_jobs", ["job_id"])
    op.create_index("ix_rebuild_jobs_cluster_id", "rebuild_jobs", ["cluster_id"])
    op.create_index("ix_rebuild_jobs_instance_name", "rebuild_jobs", ["instance_name"])
    op.create_index("ix_rebuild_jobs_status", "rebuild_jobs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_rebuild_jobs_status", table_name="rebuild_jobs")
    op.drop_index("ix_rebuild_jobs_instance_name", table_name="rebuild_jobs")
    op.drop_index("ix_rebuild_jobs_cluster_id", table_name="rebuild_jobs")
    op.drop_constraint("uq_rebuild_jobs_job_id", "rebuild_jobs", type_="unique")
    op.drop_table("rebuild_jobs")
