"""Create session audit log table"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "005_session_audit"
down_revision = "004_tenant_acl"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.String(100), nullable=False),
        sa.Column("event_type", sa.String(20), nullable=False, comment="connect | disconnect"),
        sa.Column("instance_name", sa.String(200), nullable=False),
        sa.Column("host", sa.String(200), nullable=False),
        sa.Column("project", sa.String(100), nullable=False, server_default="default"),
        sa.Column("protocol", sa.String(50), nullable=False, comment="websocket-terminal | spice-graphics"),
        sa.Column("user_id", sa.String(200), nullable=False),
        sa.Column("user_email", sa.String(200), nullable=False),
        sa.Column("user_role", sa.String(100), nullable=False, server_default="unknown"),
        # Set on disconnect.
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("download_bytes", sa.Integer(), nullable=True),
        sa.Column("upload_bytes", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_index("ix_session_audit_session_id", "session_audit_logs", ["session_id"])
    op.create_index("ix_session_audit_user_id", "session_audit_logs", ["user_id"])
    op.create_index("ix_session_audit_instance", "session_audit_logs", ["instance_name"])
    op.create_index("ix_session_audit_created", "session_audit_logs", ["created_at"])
    op.create_index("ix_session_audit_instance_host", "session_audit_logs", ["instance_name", "host"])


def downgrade() -> None:
    op.drop_index("ix_session_audit_instance_host", table_name="session_audit_logs")
    op.drop_index("ix_session_audit_created", table_name="session_audit_logs")
    op.drop_index("ix_session_audit_instance", table_name="session_audit_logs")
    op.drop_index("ix_session_audit_user_id", table_name="session_audit_logs")
    op.drop_index("ix_session_audit_session_id", table_name="session_audit_logs")
    op.drop_table("session_audit_logs")
