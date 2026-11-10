"""Let a session audit row name an integration key instead of a person.

An agent driving a guest's desktop through an API key has no human to name, and a fabricated
actor in the table a tenant reads would be indistinguishable from a real one. `user_id` and
`user_email` become nullable; readers fall back to the protocol and the key recorded in the
activity index. Dropping NOT NULL is not a rewrite and takes no lock of consequence.

Downgrade deletes those rows before restoring the constraint: an API-key session has no email to
backfill.
"""

from alembic import op
import sqlalchemy as sa


revision = "018_session_audit_agent_actor"
down_revision = "017_desktop_control_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "session_audit_logs",
        "user_id",
        existing_type=sa.String(length=200),
        nullable=True,
    )
    op.alter_column(
        "session_audit_logs",
        "user_email",
        existing_type=sa.String(length=200),
        nullable=True,
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM session_audit_logs WHERE user_id IS NULL OR user_email IS NULL"
    )
    op.alter_column(
        "session_audit_logs",
        "user_email",
        existing_type=sa.String(length=200),
        nullable=False,
    )
    op.alter_column(
        "session_audit_logs",
        "user_id",
        existing_type=sa.String(length=200),
        nullable=False,
    )
