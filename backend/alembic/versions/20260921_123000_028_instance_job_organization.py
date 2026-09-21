"""Persist target organization identity for instance operation audits.

Revision ID: 028_instance_job_organization
Revises: 027_instance_operation_ids
"""

from alembic import op
import sqlalchemy as sa


revision = "028_instance_job_organization"
down_revision = "027_instance_operation_ids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "instance_operation_jobs",
        sa.Column("target_organization_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "instance_operation_jobs",
        sa.Column("target_organization_slug", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "instance_operation_jobs",
        sa.Column(
            "target_organization_resolution",
            sa.String(length=16),
            server_default="ambiguous",
            nullable=False,
        ),
    )
    op.create_foreign_key(
        "fk_instance_operation_jobs_target_organization",
        "instance_operation_jobs",
        "organizations",
        ["target_organization_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_instance_operation_org_resolution",
        "instance_operation_jobs",
        "target_organization_resolution IN ('exact', 'ambiguous', 'unbound')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_instance_operation_org_resolution",
        "instance_operation_jobs",
        type_="check",
    )
    op.drop_constraint(
        "fk_instance_operation_jobs_target_organization",
        "instance_operation_jobs",
        type_="foreignkey",
    )
    op.drop_column("instance_operation_jobs", "target_organization_resolution")
    op.drop_column("instance_operation_jobs", "target_organization_slug")
    op.drop_column("instance_operation_jobs", "target_organization_id")
