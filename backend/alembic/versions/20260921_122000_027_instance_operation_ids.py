"""Retain every daemon operation correlated to an instance job.

Revision ID: 027_instance_operation_ids
Revises: 026_unknown_instance_jobs_are_active
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "027_instance_operation_ids"
down_revision = "026_unknown_instance_jobs_are_active"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "instance_operation_jobs",
        sa.Column(
            "operation_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("instance_operation_jobs", "operation_ids")
