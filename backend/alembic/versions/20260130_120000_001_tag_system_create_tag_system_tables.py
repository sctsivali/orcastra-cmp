"""Create the tag system tables.

tag_categories (keyed by prefix, ex; loc_, vend_, env_), tags, tag_assignments
(tags on nodes, projects and instances) and user_tag_visibility (per-user
visibility settings).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "001_tag_system"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create tag system tables."""
    
    op.create_table(
        "tag_categories",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("prefix", sa.String(10), nullable=False),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("color", sa.String(7), nullable=True, server_default="#6b7280"),
        sa.Column("icon", sa.String(50), nullable=True, server_default="tag"),
        sa.Column("sort_order", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=True, server_default="true"),
        sa.Column("is_system", sa.Boolean(), nullable=True, server_default="true"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("prefix"),
    )
    op.create_index("ix_tag_categories_prefix", "tag_categories", ["prefix"])
    
    op.create_table(
        "tags",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=True),
        sa.Column(
            "category_id",
            sa.Integer(),
            sa.ForeignKey("tag_categories.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("color", sa.String(7), nullable=True),
        sa.Column("is_system", sa.Boolean(), nullable=True, server_default="false"),
        sa.Column("usage_count", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index("ix_tags_name", "tags", ["name"])
    op.create_index("ix_tags_category_id", "tags", ["category_id"])
    op.create_index("ix_tags_created_by", "tags", ["created_by"])
    
    op.create_table(
        "tag_assignments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "tag_id",
            sa.Integer(),
            sa.ForeignKey("tags.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("resource_type", sa.String(20), nullable=False),
        sa.Column("resource_id", sa.String(255), nullable=False),
        sa.Column("resource_host", sa.String(255), nullable=True),
        sa.Column("resource_project", sa.String(255), nullable=True),
        sa.Column("assigned_by", sa.String(255), nullable=True),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tag_id", "resource_type", "resource_id", "resource_host", "resource_project",
            name="uq_tag_assignment_resource"
        ),
    )
    op.create_index("ix_tag_assignment_resource", "tag_assignments", ["resource_type", "resource_id"])
    op.create_index("ix_tag_assignment_host", "tag_assignments", ["resource_host"])
    op.create_index("ix_tag_assignment_project", "tag_assignments", ["resource_project"])
    op.create_index("ix_tag_assignment_tag_id", "tag_assignments", ["tag_id"])
    
    op.create_table(
        "user_tag_visibility",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("visibility_mode", sa.String(20), nullable=False, server_default="all"),
        sa.Column("visible_categories", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("visible_tags", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("hidden_categories", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("hidden_tags", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("default_filter_tags", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_user_tag_visibility"),
    )
    op.create_index("ix_user_tag_visibility_user_id", "user_tag_visibility", ["user_id"])
    
    op.execute("""
        INSERT INTO tag_categories (prefix, name, description, color, icon, sort_order, is_system, is_active)
        VALUES
            ('loc_', 'Location', 'Physical location or data center', '#3b82f6', 'map-pin', 1, true, true),
            ('reg_', 'Region', 'Geographic region or zone', '#06b6d4', 'globe', 2, true, true),
            ('vend_', 'Vendor', 'Vendor or provider', '#8b5cf6', 'building', 3, true, true),
            ('own_', 'Ownership', 'Asset ownership or responsibility', '#f59e0b', 'user-check', 4, true, true),
            ('env_', 'Environment', 'Deployment environment (production, staging, dev)', '#10b981', 'layers', 5, true, true),
            ('app_', 'Application', 'Application type or workload', '#ec4899', 'package', 6, true, true),
            ('sla_', 'SLA Level', 'Service level agreement tier', '#ef4444', 'shield-check', 7, true, true),
            ('cost_', 'Cost Center', 'Cost center or billing code', '#84cc16', 'wallet', 8, true, true)
        ON CONFLICT (prefix) DO NOTHING;
    """)


def downgrade() -> None:
    """Drop tag system tables."""
    op.drop_table("user_tag_visibility")
    op.drop_table("tag_assignments")
    op.drop_table("tags")
    op.drop_table("tag_categories")
