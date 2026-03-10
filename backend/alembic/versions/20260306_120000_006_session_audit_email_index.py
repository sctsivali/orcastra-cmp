"""Add index on user_email to session_audit_logs

Speeds up user-scoped queries (WHERE user_email = ?) which run on every
page load of the Sessions dashboard.
"""

from alembic import op

# revision identifiers
revision = "006_session_audit_email_idx"
down_revision = "005_session_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_session_audit_logs_user_email",
        "session_audit_logs",
        ["user_email"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_session_audit_logs_user_email", table_name="session_audit_logs")
