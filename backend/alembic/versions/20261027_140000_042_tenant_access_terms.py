"""Paid-term dates on a tenant access policy, so access ends when the purchase does.

OrcaHub provisions a buyer's access as one `auto:<project>` policy and, at the end of the paid
term, narrows it or (optionally) revokes it. Orcastra kept no record of the term, so a buyer whose
revoke never arrived kept the project, and the cluster it sits on, indefinitely. `term_ended_at`
narrows the policy to viewing once it passes; `access_ends_at` ends it outright; `term_as_of`
orders replays; `term_phase_recorded` lets the sweeper audit each boundary once.

Revision ID: 042_tenant_access_terms
Revises: 041_cluster_registrations
"""

from alembic import op
import sqlalchemy as sa

revision = "042_tenant_access_terms"
down_revision = "041_cluster_registrations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenant_access_policies", sa.Column("term_ended_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tenant_access_policies", sa.Column("access_ends_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tenant_access_policies", sa.Column("term_as_of", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "tenant_access_policies",
        sa.Column("term_phase_recorded", sa.String(length=16), nullable=True, comment="ending | read_only | ended"),
    )
    op.create_check_constraint(
        "ck_tenant_policy_access_after_term",
        "tenant_access_policies",
        "access_ends_at IS NULL OR term_ended_at IS NULL OR access_ends_at >= term_ended_at",
    )
    op.create_check_constraint(
        "ck_tenant_policy_term_phase",
        "tenant_access_policies",
        "term_phase_recorded IS NULL OR term_phase_recorded IN ('active', 'ending', 'read_only', 'ended')",
    )
    op.create_index(
        "ix_tenant_policy_term_dates",
        "tenant_access_policies",
        ["access_ends_at", "term_ended_at"],
        postgresql_where=sa.text("access_ends_at IS NOT NULL OR term_ended_at IS NOT NULL"),
    )


def downgrade() -> None:
    # important: the older code knows expiry only as an assignment's expires_at. Copy every hard
    # end there first, so a rollback never reopens access a buyer's term already closed.
    op.execute(
        """
        UPDATE tenant_policy_assignments AS a
           SET expires_at = p.access_ends_at
          FROM tenant_access_policies AS p
         WHERE a.policy_id = p.id
           AND p.access_ends_at IS NOT NULL
           AND (a.expires_at IS NULL OR a.expires_at > p.access_ends_at)
        """
    )
    op.drop_index("ix_tenant_policy_term_dates", table_name="tenant_access_policies")
    op.drop_constraint("ck_tenant_policy_term_phase", "tenant_access_policies", type_="check")
    op.drop_constraint("ck_tenant_policy_access_after_term", "tenant_access_policies", type_="check")
    op.drop_column("tenant_access_policies", "term_phase_recorded")
    op.drop_column("tenant_access_policies", "term_as_of")
    op.drop_column("tenant_access_policies", "access_ends_at")
    op.drop_column("tenant_access_policies", "term_ended_at")
