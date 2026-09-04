"""Make a grant able to name an API key, and record whether an organization consents to desktop control.

Two changes, both prerequisites for letting an AI agent see and drive a guest's desktop.

The first is that `break_glass_grants` could only ever name a person. A human admin cannot open a
console into a cluster they do not own without a justified, time-boxed, revocable grant that the
tenant can see. An API key had no equivalent and no way to acquire one: the table is keyed on
`admin_email`, and `enforce_sensitive_access` takes an authenticated user. So a partner could
issue a key, tick a capability, and hold permanent unattended access to a tenant's guests with no
reason recorded, no expiry, and nothing for the tenant to revoke. Rather than build a second
grant table with its own expiry, revocation and watchdog, the existing one learns a second kind of
principal.

`admin_email` therefore becomes nullable and `ck_bgg_one_principal` requires exactly one of the
two. A grant with neither authorizes nobody; a grant with both would be ambiguous about who it
covers, and the enforcement query would match on either.

`cluster_fingerprint` closes a separate hole. `cluster_id` is a free-text Vault key with no
foreign key anywhere in this schema, so a cluster deleted and re-registered under a different
owner matches an old grant by string alone. Pinning the server certificate fingerprint at issue
means the grant dies with the registration it was issued against, because a re-registration cannot
reproduce the key pair. Nullable, so grants issued before this migration keep working and are
simply not covered by that check.

The second change is `desktop_control_policy`. A capability on a key says what the integration may
attempt. This says whether the organization owning the cluster consents at all. They are different
consents given by different people, and screen contents leaving for a third-party model is a
disclosure to a sub-processor rather than an ordinary API call.

It gets its own table rather than a field in `organization_preferences.settings`, which is the
personalization JSONB the branding editor writes. This repository has already shipped bugs where
one code path overwrote another's keys in that column, and a security consent cleared by somebody
saving a logo is not a failure mode worth inviting back.

Both defaults are false and an absent row reads the same as false, so an organization that has
never been asked is never treated as having agreed.
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
    # important: any key-principal grant must go before admin_email can be NOT NULL again, or the
    # ALTER fails on rows this migration made possible. Deleting them is correct rather than
    # destructive: without api_key_id they no longer authorize anything.
    op.execute("DELETE FROM break_glass_grants WHERE admin_email IS NULL")
    op.alter_column(
        "break_glass_grants",
        "admin_email",
        existing_type=sa.String(length=255),
        nullable=False,
    )
