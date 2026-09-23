"""Pin current Authentik identity for durable migration authorization.

Revision ID: 033_migration_actor_identity
Revises: 032_migration_index_parity
"""

from alembic import op
import sqlalchemy as sa


revision = "033_migration_actor_identity"
down_revision = "032_migration_index_parity"
branch_labels = None
depends_on = None


def _add_columns(table_name: str) -> None:
    # Nullable only for rows created by a pre-release migration-center build. The service
    # refuses to resume a row without all four values, so absence fails closed.
    op.add_column(table_name, sa.Column("actor_issuer", sa.String(length=2048)))
    op.add_column(table_name, sa.Column("actor_subject", sa.String(length=512)))
    op.add_column(table_name, sa.Column("actor_email", sa.String(length=320)))
    op.add_column(table_name, sa.Column("actor_provider_uuid", sa.String(length=36)))


def upgrade() -> None:
    _add_columns("migration_plans")
    _add_columns("migration_jobs")


def downgrade() -> None:
    for table_name in ("migration_jobs", "migration_plans"):
        op.drop_column(table_name, "actor_provider_uuid")
        op.drop_column(table_name, "actor_email")
        op.drop_column(table_name, "actor_subject")
        op.drop_column(table_name, "actor_issuer")
