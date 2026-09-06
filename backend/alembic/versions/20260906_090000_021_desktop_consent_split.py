"""Give the clipboard and guest access their own consent.

Both used to ride on `enabled`, which is the switch an operator is told means "whether an agent
may capture this organization's screens".

Reading a guest's clipboard was authorized by that alone, while `services/desktop/actions.py`
calls the clipboard the highest-yield exfiltration primitive on the surface and the capability is
already separate on the key.

Worse, the add-on install path gated `instance.exec` and `instance.files` on the same switch. So
an organization that enabled screenshots silently authorized a third-party application to take a
shell inside a renting customer's container, and an organization that wanted an exec add-on had to
enable screen capture it did not want.

Both default to false, so this migration grants nothing. Existing rows keep view and input exactly
as they were and lose two powers they were never asked about, which is the safe direction.

Revision ID: 021_desktop_consent_split
Revises: 020_extension_webhooks
"""

import sqlalchemy as sa
from alembic import op

revision = "021_desktop_consent_split"
down_revision = "020_extension_webhooks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "desktop_control_policy",
        sa.Column("allow_clipboard", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "desktop_control_policy",
        sa.Column("allow_guest_access", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("desktop_control_policy", "allow_guest_access")
    op.drop_column("desktop_control_policy", "allow_clipboard")
