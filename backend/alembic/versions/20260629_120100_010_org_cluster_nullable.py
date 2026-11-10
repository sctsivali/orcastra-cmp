"""Make organization_clusters.organization_id nullable.

A partner who registers a cluster before their organization is synced from OrcaHub
owns it through registered_by. Until reconcile binds it to an organization the row has
organization_id NULL, so the registrant can still see it and grant API-key access to it.
A partial unique index keeps at most one unbound row per cluster; uq_org_cluster cannot,
because Postgres treats NULLs as distinct.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "010_org_cluster_nullable"
down_revision = "009_merge_org_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "organization_clusters",
        "organization_id",
        existing_type=sa.Integer(),
        nullable=True,
    )
    op.create_index(
        "uq_org_cluster_null",
        "organization_clusters",
        ["cluster_id"],
        unique=True,
        postgresql_where=sa.text("organization_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_org_cluster_null", table_name="organization_clusters")
    # important: NOT NULL cannot be restored while unbound rows exist. Dropping them is
    # destructive (not-yet-synced cluster ownership is lost), but it is the only way back.
    op.execute("DELETE FROM organization_clusters WHERE organization_id IS NULL")
    op.alter_column(
        "organization_clusters",
        "organization_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
