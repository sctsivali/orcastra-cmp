"""Create local_identities and audit_log tables for the compact (client-cert) build.

local_identities: maps a client-certificate SHA-256 fingerprint to a role + status,
replacing the Authentik directory in single-instance deployments.

audit_log: append-only, hash-chained audit sink that replaces OpenSearch when the
compact build runs without centralized logging.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision = "009_local_identities_audit"
down_revision = "008_image_upload_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "local_identities",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("common_name", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("role", sa.String(20), nullable=False, server_default="tenant"),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("serial", sa.String(128), nullable=True),
        sa.Column("not_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_unique_constraint("uq_local_identities_fingerprint", "local_identities", ["fingerprint"])
    op.create_index("ix_local_identities_fingerprint", "local_identities", ["fingerprint"])
    op.create_index("ix_local_identities_status", "local_identities", ["status"])
    # Enforce at most ONE active admin row created by bootstrap so the trust-on-first-use
    # window cannot be raced into two admins. Regular admins (created_by != 'bootstrap')
    # are added by an existing admin and are not constrained here.
    op.create_index(
        "uq_local_identities_single_bootstrap_admin",
        "local_identities",
        ["role"],
        unique=True,
        postgresql_where=sa.text("role = 'admin' AND status = 'active' AND created_by = 'bootstrap'"),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(36), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("actor_id", sa.String(255), nullable=True),
        sa.Column("actor_email", sa.String(255), nullable=True),
        sa.Column("actor_role", sa.String(50), nullable=True),
        sa.Column("action", sa.String(120), nullable=False),
        sa.Column("category", sa.String(60), nullable=True),
        sa.Column("severity", sa.String(20), nullable=True, server_default="info"),
        sa.Column("result", sa.String(20), nullable=True, server_default="success"),
        sa.Column("target_type", sa.String(60), nullable=True),
        sa.Column("target_id", sa.String(255), nullable=True),
        sa.Column("target_host", sa.String(255), nullable=True),
        sa.Column("target_project", sa.String(120), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("source_ip", sa.String(64), nullable=True),
        sa.Column("details", postgresql.JSONB, nullable=True),
        sa.Column("prev_hash", sa.String(64), nullable=True),
        sa.Column("row_hash", sa.String(64), nullable=False),
    )
    op.create_index("ix_audit_log_event_id", "audit_log", ["event_id"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])
    op.create_index("ix_audit_log_actor_id", "audit_log", ["actor_id"])
    op.create_index("ix_audit_log_occurred", "audit_log", ["occurred_at"])
    op.create_index("ix_audit_log_actor_action", "audit_log", ["actor_id", "action"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_actor_action", table_name="audit_log")
    op.drop_index("ix_audit_log_occurred", table_name="audit_log")
    op.drop_index("ix_audit_log_actor_id", table_name="audit_log")
    op.drop_index("ix_audit_log_action", table_name="audit_log")
    op.drop_index("ix_audit_log_event_id", table_name="audit_log")
    op.drop_table("audit_log")

    op.drop_index("uq_local_identities_single_bootstrap_admin", table_name="local_identities")
    op.drop_index("ix_local_identities_status", table_name="local_identities")
    op.drop_index("ix_local_identities_fingerprint", table_name="local_identities")
    op.drop_constraint("uq_local_identities_fingerprint", "local_identities", type_="unique")
    op.drop_table("local_identities")
