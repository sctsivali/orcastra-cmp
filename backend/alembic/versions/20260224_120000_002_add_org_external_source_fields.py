"""Add external source fields to organizations.

For organizations synced from an external application (OrcaHub):
- source: 'local' or 'orcahub'
- external_id: the external system's UUID, used for upsert and dedup
- api_key_id: the API key connection that synced the organization
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision: str = "002_org_external_source"
# Must follow 002_rbac_orgs, which creates the organizations table this revision
# alters. This and 003_org_prefs both branch off 002_rbac_orgs and merge again at
# 009_merge_org_heads.
down_revision: Union[str, None] = "002_rbac_orgs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add source, external_id, api_key_id columns to organizations."""
    op.add_column(
        "organizations",
        sa.Column(
            "source",
            sa.String(50),
            nullable=False,
            server_default="local",
        ),
    )
    op.add_column(
        "organizations",
        sa.Column(
            "external_id",
            sa.String(255),
            nullable=True,
        ),
    )
    op.add_column(
        "organizations",
        sa.Column(
            "api_key_id",
            sa.String(255),
            nullable=True,
        ),
    )

    op.create_unique_constraint(
        "uq_org_source_external_id",
        "organizations",
        ["source", "external_id"],
    )
    op.create_index(
        "ix_organizations_api_key_id",
        "organizations",
        ["api_key_id"],
    )
    op.create_index(
        "ix_organizations_source",
        "organizations",
        ["source"],
    )


def downgrade() -> None:
    """Remove external source fields."""
    op.drop_index("ix_organizations_source", table_name="organizations")
    op.drop_index("ix_organizations_api_key_id", table_name="organizations")
    op.drop_constraint("uq_org_source_external_id", "organizations", type_="unique")
    op.drop_column("organizations", "api_key_id")
    op.drop_column("organizations", "external_id")
    op.drop_column("organizations", "source")
