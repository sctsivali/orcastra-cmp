"""Remember the client certificates the dashboard superseded, so their trust entries can go.

Renew, regenerate and cancel each leave an LXD trust entry behind that nothing removed. This table
is the dashboard's own record of which fingerprints it replaced on which cluster, which is what
makes removing exactly those entries safe. See app/models/trust_cleanup_models.py.

Revision ID: 039_lxd_trust_cleanup
Revises: 038_organization_owner_keys
"""

from alembic import op
import sqlalchemy as sa

revision = "039_lxd_trust_cleanup"
down_revision = "038_organization_owner_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lxd_trust_cleanup",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("cluster_id", sa.String(length=255), nullable=False),
        sa.Column(
            "fingerprint",
            sa.String(length=64),
            nullable=False,
            comment="LXD form: lowercase sha256 of the DER",
        ),
        sa.Column(
            "reason",
            sa.String(length=24),
            nullable=False,
            comment="renew | verify | regenerate | cancel | pending_replaced | pending_expired | renew_aborted",
        ),
        sa.Column(
            "state",
            sa.String(length=16),
            server_default="pending",
            nullable=False,
            comment="pending | failed | removed | cancelled",
        ),
        sa.Column("identity_name", sa.String(length=255), nullable=True, comment="trust entry name, when known"),
        sa.Column("requested_by", sa.String(length=255), nullable=True, comment="actor whose action superseded it"),
        # important: server defaults on the timestamps. The sweep settles rows with raw SQL, and a
        # NOT NULL column whose only default is an ORM lambda fails the first non-ORM insert.
        sa.Column("superseded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("already_gone", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cluster_id", "fingerprint", name="uq_lxd_trust_cleanup_cluster_fp"),
        sa.CheckConstraint(
            "state IN ('pending', 'failed', 'removed', 'cancelled')",
            name="ck_lxd_trust_cleanup_state",
        ),
    )
    op.create_index("ix_lxd_trust_cleanup_cluster_id", "lxd_trust_cleanup", ["cluster_id"])
    op.create_index("ix_lxd_trust_cleanup_state_due", "lxd_trust_cleanup", ["state", "due_at"])


def downgrade() -> None:
    op.drop_index("ix_lxd_trust_cleanup_state_due", table_name="lxd_trust_cleanup")
    op.drop_index("ix_lxd_trust_cleanup_cluster_id", table_name="lxd_trust_cleanup")
    op.drop_table("lxd_trust_cleanup")
