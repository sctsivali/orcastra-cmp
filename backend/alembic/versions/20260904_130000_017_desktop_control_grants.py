"""Let a grant name an API key, and record whether an organization consents to desktop control.

`break_glass_grants` gains `api_key_id`, so an integration key needs the same justified,
time-boxed, revocable, tenant-visible grant as a human admin. `admin_email` becomes nullable and
`ck_bgg_one_principal` requires exactly one of the two: a grant with neither names no principal, and
one with both would be ambiguous. `cluster_fingerprint` pins the server certificate the grant was
issued against: `cluster_id` is free text, so a cluster re-registered under another owner would
otherwise match an old grant. It is nullable, so earlier grants keep working without that check.

`desktop_control_policy` records whether the organization owning the cluster consents at all, which
a capability on a key cannot say. It is a table of its own because the branding editor writes the
`organization_preferences.settings` JSONB, and saving a logo must never clear a security consent.
Both flags default to false and an absent row reads as false.
"""

from alembic import op
import sqlalchemy as sa


revision = "017_desktop_control_grants"
down_revision = "016_personalization_defaults"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── break_glass_grants learns a second kind of principal ───────────────────────────────────
    op.alter_column(
        "break_glass_grants",
        "admin_email",
        existing_type=sa.String(length=255),
        nullable=True,
    )
    op.add_column(
        "break_glass_grants",
        sa.Column("api_key_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "break_glass_grants",
        sa.Column("cluster_fingerprint", sa.String(length=128), nullable=True),
    )
    op.create_index(
        "ix_break_glass_grants_api_key_id", "break_glass_grants", ["api_key_id"]
    )
    op.create_index(
        "ix_bgg_key_lookup",
        "break_glass_grants",
        ["api_key_id", "cluster_id", "status", "expires_at"],
    )
    # Every existing row has admin_email set and api_key_id null, so this validates without a
    # rewrite and without needing NOT VALID plus a later VALIDATE.
    op.create_check_constraint(
        "ck_bgg_one_principal",
        "break_glass_grants",
        "(admin_email IS NOT NULL AND api_key_id IS NULL) "
        "OR (admin_email IS NULL AND api_key_id IS NOT NULL)",
    )

    # ── per-organization consent ───────────────────────────────────────────────────────────────
    op.create_table(
        "desktop_control_policy",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("allow_input", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id"),
    )
    op.create_index(
        "ix_desktop_control_policy_organization_id",
        "desktop_control_policy",
        ["organization_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_desktop_control_policy_organization_id", table_name="desktop_control_policy")
    op.drop_table("desktop_control_policy")

    op.drop_constraint("ck_bgg_one_principal", "break_glass_grants", type_="check")
    op.drop_index("ix_bgg_key_lookup", table_name="break_glass_grants")
    op.drop_index("ix_break_glass_grants_api_key_id", table_name="break_glass_grants")
    op.drop_column("break_glass_grants", "cluster_fingerprint")
    op.drop_column("break_glass_grants", "api_key_id")
    # important: key-principal grants must go before admin_email is NOT NULL again, or the ALTER
    # fails. Without api_key_id they authorize nothing, so deleting them removes no access.
    op.execute("DELETE FROM break_glass_grants WHERE admin_email IS NULL")
    op.alter_column(
        "break_glass_grants",
        "admin_email",
        existing_type=sa.String(length=255),
        nullable=False,
    )
