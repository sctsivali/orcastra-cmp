"""Add the add-on registry and per-organization installations.

An add-on is an external application holding a credential this deployment issued. These tables
record what was published and what an operator consented to; the credential stays in Vault.

Both foreign keys on `extension_installations` are RESTRICT, unlike the CASCADE of other
organization children: an installation owns Vault credential material, and a CASCADE would leave a
live key pair authenticating with nothing left to describe or find it. The teardown runs first.

`ck_ext_install_caps_nonempty` matters most. An API key with no `capabilities` field reads as
unrestricted (legacy keys), so the constraint stops any writer producing an installation
credential that grants everything. Downgrade refuses while installations exist: dropping the
table revokes nothing, and the keys would keep authenticating with no record of whose they are.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "019_extension_registry"
down_revision = "018_session_audit_agent_actor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "extensions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("publisher", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("homepage_url", sa.String(length=512), nullable=True),
        sa.Column("support_url", sa.String(length=512), nullable=True),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="draft", nullable=False),
        sa.Column("owner_email", sa.String(length=255), nullable=False),
        sa.Column("publisher_organization_id", sa.Integer(), nullable=True),
        sa.Column("requested_capabilities", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("requested_scope", postgresql.JSONB(), nullable=False),
        sa.Column("settings_schema", postgresql.JSONB(), nullable=True),
        sa.Column("default_webhook_url", sa.String(length=512), nullable=True),
        sa.Column("default_webhook_events", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        sa.Column("manifest_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["publisher_organization_id"], ["organizations.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'deprecated', 'disabled')",
            name="ck_extension_status",
        ),
        sa.CheckConstraint(
            "cardinality(requested_capabilities) > 0", name="ck_extension_requests_something"
        ),
    )
    op.create_index("ix_extensions_slug", "extensions", ["slug"], unique=True)
    op.create_index("ix_extensions_status", "extensions", ["status"])
    op.create_index("ix_extensions_owner_email", "extensions", ["owner_email"])
    op.create_index(
        "ix_extensions_publisher_organization_id", "extensions", ["publisher_organization_id"]
    )
    op.create_index("ix_extensions_owner_status", "extensions", ["owner_email", "status"])

    op.create_table(
        "extension_installations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("extension_id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column("granted_capabilities", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("granted_scope", postgresql.JSONB(), nullable=False),
        sa.Column("settings", postgresql.JSONB(), nullable=True),
        sa.Column("configured_secrets", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("api_key_id", sa.String(length=255), nullable=False),
        sa.Column("previous_api_key_id", sa.String(length=255), nullable=True),
        sa.Column("previous_key_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("credential_generation", sa.Integer(), server_default="1", nullable=False),
        sa.Column("consent_version", sa.String(length=32), nullable=False),
        sa.Column("consent_manifest_digest", sa.String(length=64), nullable=False),
        sa.Column("consented_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("installed_by", sa.String(length=255), nullable=False),
        sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suspended_by", sa.String(length=255), nullable=True),
        sa.Column("suspend_reason", sa.Text(), nullable=True),
        sa.Column("last_scope_change_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["extension_id"], ["extensions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("api_key_id"),
        sa.UniqueConstraint("extension_id", "organization_id", name="uq_extension_install"),
        sa.CheckConstraint(
            "cardinality(granted_capabilities) > 0", name="ck_ext_install_caps_nonempty"
        ),
        sa.CheckConstraint(
            "jsonb_array_length(granted_scope) > 0", name="ck_ext_install_scope_nonempty"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'suspended', 'revoked')", name="ck_ext_install_status"
        ),
    )
    op.create_index(
        "ix_extension_installations_extension_id", "extension_installations", ["extension_id"]
    )
    op.create_index(
        "ix_extension_installations_organization_id",
        "extension_installations",
        ["organization_id"],
    )
    op.create_index(
        "ix_extension_installations_status", "extension_installations", ["status"]
    )
    op.create_index(
        "ix_extension_installations_api_key_id",
        "extension_installations",
        ["api_key_id"],
        unique=True,
    )
    op.create_index(
        "ix_extension_installations_installed_by", "extension_installations", ["installed_by"]
    )
    op.create_index(
        "ix_ext_install_status_org", "extension_installations", ["status", "organization_id"]
    )
    # Partial: only rows mid-rotation carry a superseded key, and the sweep that clears them
    # scans on exactly that predicate.
    op.create_index(
        "ix_ext_install_prev_key",
        "extension_installations",
        ["previous_api_key_id"],
        postgresql_where=sa.text("previous_api_key_id IS NOT NULL"),
    )


def downgrade() -> None:
    live = op.get_bind().execute(
        sa.text("SELECT count(*) FROM extension_installations")
    ).scalar()
    if live:
        raise RuntimeError(
            f"{live} extension installation(s) exist. Dropping this table does not revoke their "
            "credentials: the keys keep authenticating and nothing is left that says whose they "
            "are. Uninstall them through the API first."
        )
    op.drop_table("extension_installations")
    op.drop_table("extensions")
