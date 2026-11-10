"""Branding follows the organization that sold to you, and the deployment has a default of its own.

A buyer follows its seller's branding live instead of keeping a one-time copy from its first
provision, and an organization without a row of its own stops showing its creator's personal
settings. This revision:

  * adds organizations.provider_organization_id (and how the link was made), filled from the
    seller's proof of sale: the seller registered the cluster (a binding whose registrant is a
    member of the seller) and the buyer's binding there came from provisioning (no registrant).
    Failing that, from a provisioning copy equal to exactly one other organization's row.
    Anything ambiguous stays unlinked;
  * adds deployment_branding, a single row, empty until an Administrator sets it;
  * turns a branded personal row an unlinked organization without a row was showing into that
    organization's own row (updated_by 'migration:045'), so its members see no change;
  * sets aside, in organization_preferences_retired, every empty '{}' row (a reset deletes the
    row) and the provisioning copy in each buyer that is now linked;
  * adds CHECK settings <> '{}' so an empty row cannot come back.

A row a person wrote is never changed or moved. Downgrade restores the set-aside rows where the
organization has no row, drops the promoted ones, gives each linked organization without a row a
copy of its provider's row, and drops the deployment default.

Revision ID: 045_branding_inheritance
Revises: 044_my_key_records
"""

import json

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "045_branding_inheritance"
down_revision = "044_my_key_records"
branch_labels = None
depends_on = None

PROMOTED_BY = "migration:045"
PROVISION_COPY_BY = "orcahub-provisioning"

# An untouched personal row, across the versions that wrote one. A row that differs only in
# tagline is not branded: earlier builds had other default taglines.
_DEFAULT_NAMES = {"orcastra", "orcastra cmp", ""}
_DEFAULT_FOOTER = "© {year} orcastra"
_DEFAULT_COLOURS = {
    "accentColor": "#8467e4",
    "sidebarBgColor": "#321f6f",
    "sidebarTextColor": "#f8fafc",
}


def _branded(settings: dict) -> bool:
    if not isinstance(settings, dict):
        return False
    logo = settings.get("logo") or {}
    background = settings.get("background") or {}
    if any(logo.get(key) for key in ("lightModeLogo", "darkModeLogo", "favicon")):
        return True
    if background.get("backgroundImage"):
        return True
    branding = settings.get("branding") or {}
    if str(branding.get("appName", "")).strip().lower() not in _DEFAULT_NAMES:
        return True
    footer = str(branding.get("footerText", "")).strip().lower()
    if footer and footer != _DEFAULT_FOOTER:
        return True
    colours = settings.get("colors") or {}
    return any(
        str(colours.get(key, default)).strip().lower() != default
        for key, default in _DEFAULT_COLOURS.items()
    )


def _branding_only(settings: dict) -> dict:
    return {key: value for key, value in settings.items() if key in ("logo", "background", "branding", "colors")}


def _add_schema() -> None:
    op.add_column("organizations", sa.Column("provider_organization_id", sa.Integer(), nullable=True))
    op.add_column("organizations", sa.Column("provider_link_source", sa.String(length=16), nullable=True))
    op.add_column("organizations", sa.Column("provider_linked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("organizations", sa.Column("provider_linked_by", sa.String(length=255), nullable=True))
    op.create_foreign_key(
        "fk_organizations_provider_organization_id",
        "organizations",
        "organizations",
        ["provider_organization_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_organizations_provider_organization_id", "organizations", ["provider_organization_id"])
    op.create_check_constraint(
        "ck_organizations_provider_not_self",
        "organizations",
        "provider_organization_id IS NULL OR provider_organization_id <> id",
    )
    op.create_check_constraint(
        "ck_organizations_provider_link_source",
        "organizations",
        "provider_link_source IS NULL OR provider_link_source IN ('provision', 'migration', 'admin')",
    )
    op.create_table(
        "deployment_branding",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("id = 1", name="ck_deployment_branding_single_row"),
        sa.CheckConstraint("settings <> '{}'::jsonb", name="ck_deployment_branding_not_empty"),
    )
    op.create_table(
        "organization_preferences_retired",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("reason IN ('empty', 'provision_copy')", name="ck_org_prefs_retired_reason"),
    )
    op.create_index(
        "ix_organization_preferences_retired_organization_id",
        "organization_preferences_retired",
        ["organization_id"],
    )
    op.create_index(
        "ix_org_member_email_lower",
        "organization_members",
        [sa.text("lower(user_email)")],
    )


def _link_by_sale(bind) -> None:
    """A buyer whose provisioned bindings all sit on clusters one seller registered."""
    bind.execute(sa.text("""
        WITH sales AS (
            SELECT DISTINCT buyer.organization_id AS buyer_id, seller.organization_id AS seller_id
            FROM organization_clusters buyer
            JOIN organization_clusters seller
              ON seller.cluster_id = buyer.cluster_id
             AND seller.organization_id <> buyer.organization_id
             AND seller.registered_by IS NOT NULL
            JOIN organization_members registrant
              ON registrant.organization_id = seller.organization_id
             AND lower(registrant.user_email) = lower(seller.registered_by)
            JOIN organizations s ON s.id = seller.organization_id AND s.is_active
            WHERE buyer.registered_by IS NULL
              AND buyer.organization_id IS NOT NULL
        ),
        single AS (
            SELECT buyer_id, min(seller_id) AS seller_id
            FROM sales GROUP BY buyer_id HAVING count(*) = 1
        )
        UPDATE organizations o
           SET provider_organization_id = single.seller_id,
               provider_link_source = 'migration',
               provider_linked_at = now(),
               provider_linked_by = :by
          FROM single
         WHERE o.id = single.buyer_id
           AND o.source = 'orcahub'
           AND o.provider_organization_id IS NULL
    """), {"by": PROMOTED_BY})


def _link_by_copy(bind) -> None:
    """A buyer still holding provisioning's copy that equals exactly one other organization's row."""
    bind.execute(sa.text("""
        WITH copies AS (
            SELECT copy.organization_id AS buyer_id, original.organization_id AS seller_id
            FROM organization_preferences copy
            JOIN organization_preferences original
              ON original.settings = copy.settings
             AND original.organization_id <> copy.organization_id
             AND original.updated_by IS DISTINCT FROM :copy_by
            WHERE copy.updated_by = :copy_by
        ),
        single AS (
            SELECT buyer_id, min(seller_id) AS seller_id
            FROM copies GROUP BY buyer_id HAVING count(*) = 1
        )
        UPDATE organizations o
           SET provider_organization_id = single.seller_id,
               provider_link_source = 'migration',
               provider_linked_at = now(),
               provider_linked_by = :by
          FROM single
         WHERE o.id = single.buyer_id
           AND o.provider_organization_id IS NULL
    """), {"copy_by": PROVISION_COPY_BY, "by": PROMOTED_BY})


def _promote_personal_rows(bind) -> None:
    """Keep what an unlinked organization without a row showed: a branded personal row."""
    orgs = bind.execute(sa.text("""
        SELECT o.id, o.created_by
        FROM organizations o
        WHERE o.provider_organization_id IS NULL
          AND NOT EXISTS (SELECT 1 FROM organization_preferences p WHERE p.organization_id = o.id)
        ORDER BY o.id
    """)).all()
    for org_id, created_by in orgs:
        candidates = []
        if created_by and created_by != "system":
            candidates.append(created_by)
        partners = bind.execute(sa.text("""
            SELECT user_email FROM organization_members
            WHERE organization_id = :org AND role = 'partner'
            ORDER BY joined_at ASC
        """), {"org": org_id}).scalars().all()
        candidates.extend(email for email in partners if email not in candidates)
        # Exact-case email match and the first non-empty row: the row members were shown.
        for email in candidates:
            settings = bind.execute(sa.text("""
                SELECT settings FROM user_preferences
                WHERE user_email = :email
                ORDER BY updated_at DESC LIMIT 1
            """), {"email": email}).scalar()
            if not settings:
                continue
            if _branded(settings):
                bind.execute(sa.text("""
                    INSERT INTO organization_preferences (organization_id, settings, updated_by, created_at, updated_at)
                    VALUES (:org, CAST(:settings AS jsonb), :by, now(), now())
                """), {"org": org_id, "settings": json.dumps(_branding_only(settings)), "by": PROMOTED_BY})
            break


def _retire(bind) -> None:
    for reason, predicate in (
        ("empty", "p.settings = '{}'::jsonb"),
        (
            "provision_copy",
            "p.updated_by = :copy_by AND EXISTS (SELECT 1 FROM organizations o "
            "WHERE o.id = p.organization_id AND o.provider_organization_id IS NOT NULL)",
        ),
    ):
        bind.execute(sa.text(f"""
            INSERT INTO organization_preferences_retired
                (organization_id, settings, updated_by, created_at, updated_at, reason, retired_at)
            SELECT p.organization_id, p.settings, p.updated_by, p.created_at, p.updated_at, :reason, now()
            FROM organization_preferences p WHERE {predicate}
        """), {"reason": reason, "copy_by": PROVISION_COPY_BY})
        bind.execute(sa.text(f"DELETE FROM organization_preferences p WHERE {predicate}"), {"copy_by": PROVISION_COPY_BY})


def upgrade() -> None:
    _add_schema()
    bind = op.get_bind()
    _link_by_sale(bind)
    _link_by_copy(bind)
    _promote_personal_rows(bind)
    _retire(bind)
    op.create_check_constraint(
        "ck_organization_preferences_not_empty",
        "organization_preferences",
        "settings <> '{}'::jsonb",
    )


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_constraint("ck_organization_preferences_not_empty", "organization_preferences", type_="check")
    bind.execute(sa.text("DELETE FROM organization_preferences WHERE updated_by = :by"), {"by": PROMOTED_BY})
    # The newest set-aside row per organization, only where no row has been written since.
    bind.execute(sa.text("""
        INSERT INTO organization_preferences (organization_id, settings, updated_by, created_at, updated_at)
        SELECT DISTINCT ON (r.organization_id)
               r.organization_id, r.settings, r.updated_by,
               coalesce(r.created_at, now()), coalesce(r.updated_at, now())
        FROM organization_preferences_retired r
        JOIN organizations o ON o.id = r.organization_id
        WHERE NOT EXISTS (SELECT 1 FROM organization_preferences p WHERE p.organization_id = r.organization_id)
        ORDER BY r.organization_id, r.retired_at DESC, r.id DESC
    """))
    bind.execute(sa.text("""
        INSERT INTO organization_preferences (organization_id, settings, updated_by, created_at, updated_at)
        SELECT o.id, provider.settings, :copy_by, now(), now()
        FROM organizations o
        JOIN organization_preferences provider ON provider.organization_id = o.provider_organization_id
        WHERE NOT EXISTS (SELECT 1 FROM organization_preferences p WHERE p.organization_id = o.id)
    """), {"copy_by": PROVISION_COPY_BY})
    op.drop_index("ix_org_member_email_lower", table_name="organization_members")
    op.drop_index(
        "ix_organization_preferences_retired_organization_id",
        table_name="organization_preferences_retired",
    )
    op.drop_table("organization_preferences_retired")
    op.drop_table("deployment_branding")
    op.drop_constraint("ck_organizations_provider_link_source", "organizations", type_="check")
    op.drop_constraint("ck_organizations_provider_not_self", "organizations", type_="check")
    op.drop_index("ix_organizations_provider_organization_id", table_name="organizations")
    op.drop_constraint("fk_organizations_provider_organization_id", "organizations", type_="foreignkey")
    op.drop_column("organizations", "provider_linked_by")
    op.drop_column("organizations", "provider_linked_at")
    op.drop_column("organizations", "provider_link_source")
    op.drop_column("organizations", "provider_organization_id")
