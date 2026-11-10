"""Create image_upload_sessions table for resumable custom-image uploads.

Tracks each image upload started through an API key (ex; by OrcaHub) across uvicorn workers
and restarts: the resumable offset, the staging file and the import result. Bytes are staged on
a host-mounted directory; the rows are the cross-worker source of truth for tus HEAD/PATCH.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "008_image_upload_sessions"
down_revision = "007_metrics_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "image_upload_sessions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("cluster_id", sa.String(255), nullable=False),
        sa.Column("project", sa.String(255), nullable=False, server_default="default"),
        sa.Column("alias", sa.String(255), nullable=False),
        sa.Column("instance_type", sa.String(32), nullable=False),
        sa.Column("image_ref", sa.String(255), nullable=True),
        sa.Column("api_key_id", sa.String(255), nullable=True),
        sa.Column("expected_sha256", sa.String(64), nullable=True),
        sa.Column("max_bytes", sa.BigInteger, nullable=False),
        sa.Column("total_bytes", sa.BigInteger, nullable=False),
        sa.Column("upload_offset", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("fingerprint", sa.String(64), nullable=True),
        sa.Column("size", sa.BigInteger, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("staging_path", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_unique_constraint("uq_image_upload_sessions_session_id", "image_upload_sessions", ["session_id"])
    op.create_index("ix_image_upload_sessions_cluster_id", "image_upload_sessions", ["cluster_id"])
    op.create_index("ix_image_upload_sessions_status", "image_upload_sessions", ["status"])
    op.create_index("ix_image_upload_sessions_expires_at", "image_upload_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_image_upload_sessions_expires_at", table_name="image_upload_sessions")
    op.drop_index("ix_image_upload_sessions_status", table_name="image_upload_sessions")
    op.drop_index("ix_image_upload_sessions_cluster_id", table_name="image_upload_sessions")
    op.drop_constraint("uq_image_upload_sessions_session_id", "image_upload_sessions", type_="unique")
    op.drop_table("image_upload_sessions")
