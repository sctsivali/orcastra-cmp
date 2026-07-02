"""Merge the two dangling alembic heads into one.

008_image_upload_sessions (the main RBAC/feature chain) and 002_org_external_source
(org external-source fields) both branched off 001_tag_system and were never merged,
so `alembic upgrade head` fails with "multiple heads". This empty merge revision
collapses them so a single linear head exists again.
"""

from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401

# revision identifiers
revision = "009_merge_org_heads"
down_revision = ("008_image_upload_sessions", "002_org_external_source")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
