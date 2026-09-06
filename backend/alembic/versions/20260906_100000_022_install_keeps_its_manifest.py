"""An installation keeps the manifest it agreed to.

An add-on may now publish a new version, which replaces the manifest on the extension row. The
installation already records `consent_version` and `consent_manifest_digest`, so what it agreed to
is identified, but the document itself was overwritten and could not be read back. That is the
question an audit asks: not "which digest" but "what did this organization actually approve".

One nullable column, no backfill. Rows written before this keep their digest and lose only the
text, which is the state they were already in.

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
