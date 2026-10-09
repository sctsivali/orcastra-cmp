"""Give a synced organization a set of owner keys.

Revision ID: 038_organization_owner_keys
Revises: 037_migration_unknown_phase

`organizations.api_key_id` stays the primary owner (the key OrcaHub callbacks use). Every key in
`organization_owner_keys` may sync the organization, write its members and policies and provision
tenant access for it. Every organization that has an owner today gets that key as its one row, so
nothing behaves differently until a second key is added.

On PostgreSQL a deferrable composite foreign key from organizations(id, api_key_id) to the owner
rows makes the database refuse a primary that is not in the set. A NULL primary (an unclaimed
organization) is not checked (MATCH SIMPLE). It is deferred so an organization and its first owner
row can be written in one transaction in either order.

important: deploy with old workers drained. A worker without this code would insert a claimed
organization without its owner row, which the foreign key refuses at commit.
"""

import sqlalchemy as sa
from alembic import op


revision = "038_organization_owner_keys"
down_revision = "037_migration_unknown_phase"
branch_labels = None
depends_on = None

_PRIMARY_FK = "fk_org_primary_owner_key"


def upgrade() -> None:
    op.create_table(
        "organization_owner_keys",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("api_key_id", sa.String(255), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("added_via", sa.String(16), nullable=False, server_default="claim"),
        sa.Column("added_by", sa.String(255), nullable=True),
        sa.Column("proof_key_id", sa.String(255), nullable=True),
        sa.UniqueConstraint("organization_id", "api_key_id", name="uq_org_owner_key"),
        sa.CheckConstraint(
            "added_via IN ('backfill', 'claim', 'provision', 'enroll', 'admin', 'transfer', 'carry')",
            name="ck_org_owner_key_added_via",
        ),
    )
    op.create_index("ix_organization_owner_keys_api_key_id", "organization_owner_keys", ["api_key_id"])
    op.create_index("ix_org_owner_key_org_id", "organization_owner_keys", ["organization_id"])

    # Every source and inactive organizations too: an inactive organization still counts as claimed
    # by its key (integration_cluster_rebind.claimed_organization_count).
    op.execute(
        "INSERT INTO organization_owner_keys (organization_id, api_key_id, added_at, added_via, added_by) "
        "SELECT id, api_key_id, created_at, 'backfill', 'migration' FROM organizations "
        "WHERE api_key_id IS NOT NULL"
    )

    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            f"ALTER TABLE organizations ADD CONSTRAINT {_PRIMARY_FK} "
            "FOREIGN KEY (id, api_key_id) REFERENCES organization_owner_keys (organization_id, api_key_id) "
            "DEFERRABLE INITIALLY DEFERRED"
        )


def downgrade() -> None:
    # The primary stays in organizations.api_key_id, so code without owner sets keeps working; only
    # the co-owners are lost.
    if op.get_bind().dialect.name == "postgresql":
        op.execute(f"ALTER TABLE organizations DROP CONSTRAINT IF EXISTS {_PRIMARY_FK}")
    op.drop_index("ix_org_owner_key_org_id", table_name="organization_owner_keys")
    op.drop_index("ix_organization_owner_keys_api_key_id", table_name="organization_owner_keys")
    op.drop_table("organization_owner_keys")
