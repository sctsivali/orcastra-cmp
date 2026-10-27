"""Tenant access rules the audit found naming nothing, for a partner to review.

Revision ID: 043_tenant_rule_observations
Revises: 042_tenant_access_terms
"""

from alembic import op
import sqlalchemy as sa

revision = "043_tenant_rule_observations"
down_revision = "042_tenant_access_terms"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_rule_observations",
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("cluster_id", sa.String(length=255), nullable=False),
        sa.Column("project_name", sa.String(length=255), nullable=False),
        sa.Column("instance_name", sa.String(length=255), nullable=False),
        sa.Column("first_missing_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_missing_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("misses", sa.Integer(), server_default="1", nullable=False),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["rule_id"], ["tenant_policy_rules.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("rule_id"),
    )
    op.create_index("ix_tenant_rule_observations_cluster_id", "tenant_rule_observations", ["cluster_id"])


def downgrade() -> None:
    op.drop_index("ix_tenant_rule_observations_cluster_id", table_name="tenant_rule_observations")
    op.drop_table("tenant_rule_observations")
