"""Add audit_log_anchor (single-row high-water mark for tail-truncation detection).

The audit hash chain detects in-place edits and reordering but not deletion of a
contiguous tail (a shorter chain still verifies). /audit-log/verify advances this anchor
(max_id, row_count, head_hash) when the chain is intact; a later verify that sees fewer
rows or a lower max id than the anchor reveals that rows were deleted.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "011_audit_log_anchor"
down_revision = "010_audit_event_id_width"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_log_anchor",
        sa.Column("id", sa.Integer(), primary_key=True),  # always 1
        sa.Column("max_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("row_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("head_hash", sa.String(64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("audit_log_anchor")
