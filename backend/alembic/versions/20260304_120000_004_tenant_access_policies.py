"""Create tenant access policy tables"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers
revision = "004_tenant_acl"
down_revision = "003_org_prefs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Reusable access policies (like AWS IAM Policies)
    op.create_table(
        "tenant_access_policies",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_default", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("source", sa.String(50), server_default="local", nullable=False),
        sa.Column("external_id", sa.String(255), nullable=True),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_unique_constraint(
        "uq_tenant_policy_org_name",
        "tenant_access_policies",
        ["organization_id", "name"],
    )
    op.create_index(
        "ix_tenant_policy_org_id",
        "tenant_access_policies",
        ["organization_id"],
    )
    op.create_index(
        "ix_tenant_policy_external_id",
        "tenant_access_policies",
        ["external_id"],
        unique=False,
    )

    # 2. Rules within a policy (cluster/project/instance scope + permissions)
    op.create_table(
        "tenant_policy_rules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "policy_id",
            sa.Integer(),
            sa.ForeignKey("tenant_access_policies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("cluster_id", sa.String(255), nullable=False),
        sa.Column("project_name", sa.String(255), server_default="*", nullable=False),
        sa.Column("instance_name", sa.String(255), server_default="*", nullable=False),
        sa.Column(
            "permissions",
            JSONB,
            server_default='["view"]',
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_unique_constraint(
        "uq_policy_rule_scope",
        "tenant_policy_rules",
        ["policy_id", "cluster_id", "project_name", "instance_name"],
    )
    op.create_index(
        "ix_policy_rule_policy_id",
        "tenant_policy_rules",
        ["policy_id"],
    )
    op.create_index(
        "ix_policy_rule_cluster_id",
        "tenant_policy_rules",
        ["cluster_id"],
    )

    # 3. Assign policies to org members
    op.create_table(
        "tenant_policy_assignments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "policy_id",
            sa.Integer(),
            sa.ForeignKey("tenant_access_policies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "org_member_id",
            sa.Integer(),
            sa.ForeignKey("organization_members.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("assigned_by", sa.String(255), nullable=False),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(50), server_default="local", nullable=False),
        sa.Column("external_id", sa.String(255), nullable=True),
    )
    op.create_unique_constraint(
        "uq_policy_assignment",
        "tenant_policy_assignments",
        ["policy_id", "org_member_id"],
    )
    op.create_index(
        "ix_policy_assign_policy_id",
        "tenant_policy_assignments",
        ["policy_id"],
    )
    op.create_index(
        "ix_policy_assign_member_id",
        "tenant_policy_assignments",
        ["org_member_id"],
    )
    op.create_index(
        "ix_policy_assign_external_id",
        "tenant_policy_assignments",
        ["external_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("tenant_policy_assignments")
    op.drop_table("tenant_policy_rules")
    op.drop_table("tenant_access_policies")
