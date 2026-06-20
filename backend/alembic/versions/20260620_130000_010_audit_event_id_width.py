"""Widen audit_log.event_id to varchar(64).

generate_event_id() returns "evt_<uuid4>" (40 chars); the initial column was varchar(36)
(sized for a bare uuid), which truncated every audit insert. Widen to 64.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "010_audit_event_id_width"
down_revision = "009_local_identities_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "audit_log", "event_id",
        existing_type=sa.String(36),
        type_=sa.String(64),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "audit_log", "event_id",
        existing_type=sa.String(64),
        type_=sa.String(36),
        existing_nullable=False,
    )
