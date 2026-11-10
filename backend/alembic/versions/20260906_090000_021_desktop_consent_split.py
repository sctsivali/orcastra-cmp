"""Give the clipboard and guest access their own consent.

`allow_clipboard` and `allow_guest_access` take two powers off `enabled`, the switch an operator
reads as "whether an agent may capture this organization's screens". The clipboard is a high-yield
exfiltration path (`services/desktop/actions.py`), and the add-on install path gates
`instance.exec` and `instance.files` on guest access, so enabling screenshots does not authorize a
shell inside a customer's container. Both default to false: existing rows keep view and input and
gain nothing.

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
