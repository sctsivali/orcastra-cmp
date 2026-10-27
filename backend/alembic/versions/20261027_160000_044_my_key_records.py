"""A registry of My Keys, and an index for reading a key's audit rows.

The audit of a key (created, downloaded, deleted) is written to cluster_log like every other
audit record. The registry keeps a deleted key's owner so its history stays the owner's, and maps
the certificate fingerprint LXD reports as a requestor back to the key.

Revision ID: 044_my_key_records
Revises: 043_tenant_rule_observations
"""

from alembic import op
import sqlalchemy as sa

revision = "044_my_key_records"
down_revision = "043_tenant_rule_observations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "my_key_records",
        sa.Column("key_id", sa.String(length=255), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("owner_email", sa.String(length=255), nullable=True),
        sa.Column("cluster_id", sa.String(length=255), nullable=False),
        sa.Column("project", sa.String(length=255), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("auth_level", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.String(length=255), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_action", sa.String(length=96), nullable=True),
        sa.Column("last_used_cluster", sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint("key_id"),
        sa.UniqueConstraint("fingerprint", name="uq_my_key_records_fingerprint"),
    )
    op.create_index("ix_my_key_records_owner_email", "my_key_records", ["owner_email"])
    op.create_index("ix_my_key_records_cluster_id", "my_key_records", ["cluster_id"])
    op.create_index(
        "ix_cluster_log_key_rows",
        "cluster_log",
        ["resource_name", sa.text("ts DESC")],
        postgresql_where=sa.text("resource_type IN ('ssh_key', 'ssh_key_cleanup')"),
    )


def downgrade() -> None:
    op.drop_index("ix_cluster_log_key_rows", table_name="cluster_log")
    op.drop_index("ix_my_key_records_cluster_id", table_name="my_key_records")
    op.drop_index("ix_my_key_records_owner_email", table_name="my_key_records")
    op.drop_table("my_key_records")
