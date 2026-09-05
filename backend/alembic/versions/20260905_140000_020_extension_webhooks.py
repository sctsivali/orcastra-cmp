"""Durable outbound webhook delivery for add-ons.

Replaces a detached task with a row. The emitter this generalises created an unreferenced
`asyncio.create_task` per event, which CPython may collect mid-flight, held a database session
across up to three ten-second HTTP calls, and performed two blocking Vault reads per recipient on
the event loop. None of that is visible when it goes wrong: the event simply does not arrive, and
there is nothing to look at afterwards.

Rows are claimed with FOR UPDATE SKIP LOCKED, which is this schema's first use of it. Leader
election would be worse here than no coordination at all: the existing lease fails open by
design, so during a Redis outage all four workers would believe they lead and every event would
be delivered four times.

The unique constraint on (subscription_id, event_id) is what makes an enqueue safe to retry, and
`event_id` stays stable across attempts because it is the receiver's idempotency key. Delivery is
at-least-once and says so.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "020_extension_webhooks"
down_revision = "019_extension_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "extension_webhook_subscriptions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("installation_id", sa.Integer(), nullable=False),
        sa.Column("endpoint_url", sa.String(length=512), nullable=False),
        sa.Column("event_types", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("secret_fingerprint", sa.String(length=16), nullable=False),
        sa.Column("previous_secret_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status", sa.String(length=24), server_default="pending_verification", nullable=False
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_reason", sa.Text(), nullable=True),
        sa.Column("suspended_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["installation_id"], ["extension_installations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("cardinality(event_types) > 0", name="ck_subscription_has_events"),
        sa.CheckConstraint(
            "status IN ('pending_verification', 'active', 'suspended', 'disabled')",
            name="ck_subscription_status",
        ),
    )
    op.create_index(
        "ix_extension_webhook_subscriptions_installation_id",
        "extension_webhook_subscriptions",
        ["installation_id"],
    )
    op.create_index(
        "ix_extension_webhook_subscriptions_status",
        "extension_webhook_subscriptions",
        ["status"],
    )
    op.create_index(
        "ix_subscription_install_status",
        "extension_webhook_subscriptions",
        ["installation_id", "status"],
    )

    op.create_table(
        "extension_webhook_deliveries",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("attempt", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_by", sa.String(length=64), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_ms", sa.Integer(), nullable=True),
        sa.Column("response_excerpt", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["extension_webhook_subscriptions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("subscription_id", "event_id", name="uq_delivery_event"),
        sa.CheckConstraint(
            "state IN ('pending', 'inflight', 'delivered', 'failed', 'dead', 'dropped')",
            name="ck_delivery_state",
        ),
    )
    op.create_index(
        "ix_extension_webhook_deliveries_subscription_id",
        "extension_webhook_deliveries",
        ["subscription_id"],
    )
    op.create_index(
        "ix_extension_webhook_deliveries_event_type",
        "extension_webhook_deliveries",
        ["event_type"],
    )
    # The claim query orders on exactly this pair.
    op.create_index(
        "ix_delivery_claimable", "extension_webhook_deliveries", ["state", "next_attempt_at"]
    )
    # The reaper scans only rows a worker died holding.
    op.create_index(
        "ix_delivery_stuck",
        "extension_webhook_deliveries",
        ["locked_at"],
        postgresql_where=sa.text("state = 'inflight'"),
    )


def downgrade() -> None:
    op.drop_table("extension_webhook_deliveries")
    op.drop_table("extension_webhook_subscriptions")
