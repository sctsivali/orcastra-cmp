"""Durable project-delete follow-ups, and API key usage kept out of the key document.

A project delete whose outcome was unknown was followed in the worker's memory, so a restart lost
the follow-up and the access rules it was going to revoke. `project_delete_followups` holds that
work where any worker can finish it (app/models/project_delete_models.py). `api_key_usage` takes
`last_used_at` off the request path's whole-document Vault rewrite
(app/models/api_key_usage_models.py).

Revision ID: 040_followups_and_key_usage
Revises: 039_lxd_trust_cleanup
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "040_followups_and_key_usage"
down_revision = "039_lxd_trust_cleanup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_delete_followups",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("cluster_id", sa.String(length=255), nullable=False),
        sa.Column("project_name", sa.String(length=255), nullable=False),
        sa.Column("origin", sa.String(length=8), nullable=False, comment="route | proxy"),
        sa.Column("requester", sa.String(length=255), nullable=True, comment="user id or API key id that asked"),
        sa.Column(
            "scope_project",
            sa.String(length=255),
            nullable=True,
            comment="the proxy request's project, for its event",
        ),
        sa.Column(
            "event_id",
            sa.String(length=64),
            nullable=False,
            comment="stable id for every announcement of this delete",
        ),
        sa.Column(
            "rule_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "state",
            sa.String(length=12),
            server_default="sending",
            nullable=False,
            comment="sending | pending | settled | done | expired",
        ),
        sa.Column("locked_by", sa.String(length=96), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        # important: server defaults on the timestamps. The sweeper settles rows with plain UPDATEs,
        # and a NOT NULL column whose only default is an ORM lambda fails the first non-ORM insert.
        sa.Column("next_check_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "state IN ('sending', 'pending', 'settled', 'done', 'expired')",
            name="ck_project_delete_followups_state",
        ),
        sa.CheckConstraint("origin IN ('route', 'proxy')", name="ck_project_delete_followups_origin"),
    )
    op.create_index("ix_project_delete_followups_cluster_id", "project_delete_followups", ["cluster_id"])
    op.create_index(
        "ix_project_delete_followups_due", "project_delete_followups", ["state", "next_check_at"]
    )
    op.create_index(
        "uq_project_delete_followup_open",
        "project_delete_followups",
        ["cluster_id", "project_name"],
        unique=True,
        postgresql_where=sa.text("state IN ('sending', 'pending')"),
    )

    op.create_table(
        "api_key_usage",
        sa.Column("key_id", sa.String(length=255), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("key_id"),
    )


def downgrade() -> None:
    op.drop_table("api_key_usage")
    op.drop_index("uq_project_delete_followup_open", table_name="project_delete_followups")
    op.drop_index("ix_project_delete_followups_due", table_name="project_delete_followups")
    op.drop_index("ix_project_delete_followups_cluster_id", table_name="project_delete_followups")
    op.drop_table("project_delete_followups")
