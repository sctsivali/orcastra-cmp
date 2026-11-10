"""Registration generations, so an API key grant reaches only the registration it was given for.

Without them, a cluster id deregistered and registered again, possibly by another organization,
would be reached by every key grant still naming the id. See
app/models/cluster_registration_models.py.

Revision ID: 041_cluster_registrations
Revises: 040_followups_and_key_usage
"""

from alembic import op
import sqlalchemy as sa

revision = "041_cluster_registrations"
down_revision = "040_followups_and_key_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cluster_registrations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("cluster_id", sa.String(length=255), nullable=False),
        sa.Column("registration_id", sa.String(length=64), nullable=False),
        sa.Column("legacy", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_reason", sa.String(length=32), nullable=True, comment="deregistered | replaced"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("registration_id", name="uq_cluster_registrations_registration_id"),
    )
    op.create_index("ix_cluster_registrations_cluster_id", "cluster_registrations", ["cluster_id"])
    op.create_index(
        "uq_cluster_registration_active",
        "cluster_registrations",
        ["cluster_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.create_table(
        "cluster_registration_adoption",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("adopted", sa.Integer(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("cluster_registration_adoption")
    op.drop_index("uq_cluster_registration_active", table_name="cluster_registrations")
    op.drop_index("ix_cluster_registrations_cluster_id", table_name="cluster_registrations")
    op.drop_table("cluster_registrations")
