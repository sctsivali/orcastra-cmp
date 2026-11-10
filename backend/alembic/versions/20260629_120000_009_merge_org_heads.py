"""Merge the two alembic heads into one.

008_image_upload_sessions (the main chain) and 002_org_external_source both branch off
002_rbac_orgs. Without this empty merge revision `alembic upgrade head` fails with
"multiple heads".
"""

from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401

# revision identifiers
revision = "009_merge_org_heads"
# important: at the merge node alembic orders the branches by this tuple, not by file
# name or revision-map insertion order. The branch listed LAST runs FIRST on upgrade, so
# reordering the entries silently changes the order migrations apply on a fresh install.
# Real ordering requirements belong in each revision's own down_revision.
down_revision = ("008_image_upload_sessions", "002_org_external_source")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
