"""Create metrics_snapshots table for persistent resource metrics history.

Time-series resource metrics from the background collector (about every 30
seconds), kept across backend restarts so charts have no gaps.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers
revision = "007_metrics_snapshots"
down_revision = "006_session_audit_email_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "metrics_snapshots",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "cpu_usage_percent", sa.Integer, nullable=False, server_default="0",
            comment="CPU usage in basis points (value / 100 = percent)",
        ),
        sa.Column(
            "memory_usage_percent", sa.Integer, nullable=False, server_default="0",
            comment="Memory usage in basis points",
        ),
        sa.Column(
            "storage_usage_percent", sa.Integer, nullable=False, server_default="0",
            comment="Storage usage in basis points",
        ),
        sa.Column("network_rx_bytes", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("network_tx_bytes", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("disk_io_bytes", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("running_instances", sa.Integer, nullable=False, server_default="0"),
        sa.Column("nodes", JSONB, nullable=True, comment="Per-node metrics breakdown"),
        sa.Column(
            "scope", sa.String(50), nullable=False, server_default="all",
            comment="Scope key: 'all' for global metrics",
        ),
        sa.Column(
            "collected_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="When the metrics were collected",
        ),
    )

    # Primary query path: recent snapshots for a scope.
    op.create_index(
        "ix_metrics_snap_scope_time",
        "metrics_snapshots",
        ["scope", "collected_at"],
    )

    # Retention cleanup deletes by age.
    op.create_index(
        "ix_metrics_snap_collected",
        "metrics_snapshots",
        ["collected_at"],
    )

    op.create_index(
        "ix_metrics_snap_scope",
        "metrics_snapshots",
        ["scope"],
    )


def downgrade() -> None:
    op.drop_index("ix_metrics_snap_scope", table_name="metrics_snapshots")
    op.drop_index("ix_metrics_snap_collected", table_name="metrics_snapshots")
    op.drop_index("ix_metrics_snap_scope_time", table_name="metrics_snapshots")
    op.drop_table("metrics_snapshots")
