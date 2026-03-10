"""Create organization tables for three-tier RBAC"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "002_rbac_orgs"
down_revision = "001_tag_system"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # organizations
    op.create_table(
        "organizations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_organizations_slug", "organizations", ["slug"], unique=True)

    # organization_members
    op.create_table(
        "organization_members",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_email", sa.String(255), nullable=False),
        sa.Column("role", sa.String(20), nullable=False, server_default="tenant"),
        sa.Column("invited_by", sa.String(255), nullable=True),
        sa.Column("joined_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_org_member_org_id", "organization_members", ["organization_id"])
    op.create_index("ix_org_member_email", "organization_members", ["user_email"])
    op.create_unique_constraint("uq_org_member_email", "organization_members", ["organization_id", "user_email"])

    # organization_clusters
    op.create_table(
        "organization_clusters",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("cluster_id", sa.String(255), nullable=False),
        sa.Column("registered_by", sa.String(255), nullable=True),
        sa.Column("registered_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_org_cluster_org_id", "organization_clusters", ["organization_id"])
    op.create_index("ix_org_cluster_cluster_id", "organization_clusters", ["cluster_id"])
    op.create_unique_constraint("uq_org_cluster", "organization_clusters", ["organization_id", "cluster_id"])


def downgrade() -> None:
    op.drop_table("organization_clusters")
    op.drop_table("organization_members")
    op.drop_table("organizations")
