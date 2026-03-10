"""Add external source fields to organizations table

Extends the organizations table to support organizations synced from
external applications (OrcaHub). Adds:
- source: 'local' or 'orcahub' (where the org originates)
- external_id: UUID from the external system (for upsert/dedup)
- api_key_id: which API key connection brought this org
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers
revision: str = "002_org_external_source"
down_revision: Union[str, None] = "001_tag_system"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add source, external_id, api_key_id columns to organizations."""
    # source: where the org comes from ('local' created in Orcastra, 'orcahub' synced from OrcaHub)
    op.add_column(
        "organizations",
        sa.Column(
            "source",
            sa.String(50),
            nullable=False,
            server_default="local",
        ),
    )
    # external_id: the UUID from OrcaHub or other external system (nullable for local orgs)
    op.add_column(
        "organizations",
        sa.Column(
            "external_id",
            sa.String(255),
            nullable=True,
        ),
    )
    # api_key_id: which API key connection synced this org (nullable for local orgs)
    op.add_column(
        "organizations",
        sa.Column(
            "api_key_id",
            sa.String(255),
            nullable=True,
        ),
    )

    # unique constraint: one external org per source+external_id combo
    op.create_unique_constraint(
        "uq_org_source_external_id",
        "organizations",
        ["source", "external_id"],
    )
    # index for lookups by api_key_id
    op.create_index(
        "ix_organizations_api_key_id",
        "organizations",
        ["api_key_id"],
    )
    # index for lookups by source
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
