"""Create organization_preferences table"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers
revision = "003_org_prefs"
down_revision = "002_rbac_orgs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "organization_preferences",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("settings", JSONB, nullable=False, server_default="{}"),
        sa.Column("updated_by", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_org_preferences_org_id",
        "organization_preferences",
        ["organization_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_org_preferences_org_id")
    op.drop_table("organization_preferences")
