"""Break-glass grants, and organization and grant attribution on session audit rows.

Adds break_glass_grants (time-boxed, justified admin access to a tenant's console or
terminal) and three columns on session_audit_logs that attribute every session to a
tenant organization and, for a platform admin, to the grant that authorized the access.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "011_break_glass"
down_revision = "010_org_cluster_nullable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "break_glass_grants",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("admin_email", sa.String(255), nullable=False),
        sa.Column("cluster_id", sa.String(255), nullable=False),
        sa.Column(
            "target_organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("target_org_slug", sa.String(100), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("ticket_ref", sa.String(255), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("approved_by", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.String(255), nullable=True),
    )
    op.create_index("ix_bgg_admin_email", "break_glass_grants", ["admin_email"])
    op.create_index("ix_bgg_cluster_id", "break_glass_grants", ["cluster_id"])
    op.create_index("ix_bgg_expires_at", "break_glass_grants", ["expires_at"])
    op.create_index(
        "ix_bgg_active_lookup", "break_glass_grants",
        ["admin_email", "cluster_id", "status", "expires_at"],
    )
    op.create_index("ix_bgg_org", "break_glass_grants", ["target_organization_id"])
    op.create_index("ix_bgg_created", "break_glass_grants", ["created_at"])

    # Accountability columns on the session audit trail
    op.add_column(
        "session_audit_logs",
        sa.Column(
            "target_organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("session_audit_logs", sa.Column("target_org_slug", sa.String(100), nullable=True))
    op.add_column(
        "session_audit_logs",
        sa.Column(
            "break_glass_grant_id",
            sa.Integer(),
            sa.ForeignKey("break_glass_grants.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_session_audit_target_org", "session_audit_logs", ["target_organization_id"])
    op.create_index("ix_session_audit_bgg", "session_audit_logs", ["break_glass_grant_id"])


def downgrade() -> None:
    op.drop_index("ix_session_audit_bgg", table_name="session_audit_logs")
    op.drop_index("ix_session_audit_target_org", table_name="session_audit_logs")
    op.drop_column("session_audit_logs", "break_glass_grant_id")
    op.drop_column("session_audit_logs", "target_org_slug")
    op.drop_column("session_audit_logs", "target_organization_id")

    op.drop_index("ix_bgg_created", table_name="break_glass_grants")
    op.drop_index("ix_bgg_org", table_name="break_glass_grants")
    op.drop_index("ix_bgg_active_lookup", table_name="break_glass_grants")
    op.drop_index("ix_bgg_expires_at", table_name="break_glass_grants")
    op.drop_index("ix_bgg_cluster_id", table_name="break_glass_grants")
    op.drop_index("ix_bgg_admin_email", table_name="break_glass_grants")
    op.drop_table("break_glass_grants")
