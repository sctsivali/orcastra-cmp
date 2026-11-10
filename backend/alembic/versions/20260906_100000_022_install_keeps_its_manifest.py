"""An installation keeps the manifest it agreed to.

Publishing a new add-on version replaces the manifest on the extension row.
`consent_version` and `consent_manifest_digest` identify what an installation approved;
`consented_manifest` keeps the document itself, so an audit can read it back. One nullable column,
no backfill: earlier rows keep only their digest.

Revision ID: 022_install_keeps_its_manifest
Revises: 021_desktop_consent_split
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "022_install_keeps_its_manifest"
down_revision = "021_desktop_consent_split"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "extension_installations",
        sa.Column(
            "consented_manifest",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment=(
                "the manifest as it stood when this installation consented, kept because a "
                "version bump replaces the one on the extension row"
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("extension_installations", "consented_manifest")
