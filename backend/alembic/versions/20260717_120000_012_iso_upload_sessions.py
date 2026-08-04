"""Create iso_upload_sessions table for resumable custom-ISO uploads.

Mirrors image_upload_sessions (008), but for ISO files imported as LXD custom storage volumes
(content-type iso) into a chosen pool. Cross-worker source of truth for the tus HEAD/PATCH
resumable offset and the import result.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "012_iso_upload_sessions"
down_revision = "011_break_glass"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "iso_upload_sessions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("cluster_id", sa.String(255), nullable=False),
        sa.Column("project", sa.String(255), nullable=False, server_default="default"),
        sa.Column("pool", sa.String(255), nullable=False),
        sa.Column("volume_name", sa.String(255), nullable=False),
        sa.Column("iso_ref", sa.String(255), nullable=True),
        sa.Column("api_key_id", sa.String(255), nullable=True),
        sa.Column("expected_sha256", sa.String(64), nullable=True),
        sa.Column("max_bytes", sa.BigInteger, nullable=False),
        sa.Column("total_bytes", sa.BigInteger, nullable=False),
        sa.Column("upload_offset", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("size", sa.BigInteger, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("staging_path", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_unique_constraint("uq_iso_upload_sessions_session_id", "iso_upload_sessions", ["session_id"])
    op.create_index("ix_iso_upload_sessions_cluster_id", "iso_upload_sessions", ["cluster_id"])
    op.create_index("ix_iso_upload_sessions_status", "iso_upload_sessions", ["status"])
    op.create_index("ix_iso_upload_sessions_expires_at", "iso_upload_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_iso_upload_sessions_expires_at", table_name="iso_upload_sessions")
    op.drop_index("ix_iso_upload_sessions_status", table_name="iso_upload_sessions")
    op.drop_index("ix_iso_upload_sessions_cluster_id", table_name="iso_upload_sessions")
    op.drop_constraint("uq_iso_upload_sessions_session_id", "iso_upload_sessions", type_="unique")
    op.drop_table("iso_upload_sessions")
