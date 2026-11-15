"""Record who granted a binding an administrator made without registering the cluster.

An administrator's bind of an OrcaHub organization wrote no registrant, the shape provisioning
writes for a sale, so every sale reader (core/sale_term.py, org_scope._query_owned_clusters,
cluster_parties) treated the grant as a purchase that had already ended. A nullable column names
the administrator; a row with neither registrant nor grantor stays a sale. Existing rows are not
changed: no binding made so far can be told apart from a sale after the fact.

Revision ID: 047_org_cluster_granted_by
Revises: 046_forget_deregistered_records
"""

from alembic import op
import sqlalchemy as sa

revision = "047_org_cluster_granted_by"
down_revision = "046_forget_deregistered_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("organization_clusters", sa.Column("granted_by", sa.String(length=255), nullable=True))


def downgrade() -> None:
    # The grants become indistinguishable from sales again; the bindings themselves stay.
    op.drop_column("organization_clusters", "granted_by")
