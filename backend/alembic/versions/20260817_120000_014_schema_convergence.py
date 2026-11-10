"""Converge legacy create_all schemas with the migration-built schema.

A database whose schema came from `Base.metadata.create_all` at startup lacks objects
only the migrations declare: `user_preferences`, `uq_org_source_external_id` (the dedup
key of the OrcaHub sync in `routers/integrations.py`) and `ix_organizations_source` on
`organizations`, `ix_tag_assignment_tag_id`, and NOT NULL on two boolean columns.
Cosmetic differences (server defaults the ORM always fills, same-column indexes under
other names) are left alone: they change no behaviour and would only lengthen the lock.

important: every step is guarded by an inspector check. This revision runs against three
starting shapes, so each object may or may not exist: a fresh migration-built database,
one stamped by `app/core/db_bootstrap.py`, and a mixed create_all-plus-migrations one.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision = "014_schema_convergence"
down_revision = "013_rebuild_jobs"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def _has_index(table: str, name: str) -> bool:
    if not _has_table(table):
        return False
    return any(ix["name"] == name for ix in sa.inspect(op.get_bind()).get_indexes(table))


def _has_unique_constraint(table: str, name: str) -> bool:
    if not _has_table(table):
        return False
    inspector = sa.inspect(op.get_bind())
    if any(uc["name"] == name for uc in inspector.get_unique_constraints(table)):
        return True
    # create_all expresses some uniqueness as a unique index rather than a constraint,
    # so a same-named unique index counts as already present.
    return any(ix["name"] == name and ix.get("unique") for ix in inspector.get_indexes(table))


def _is_nullable(table: str, column: str) -> bool:
    if not _has_table(table):
        return False
    for col in sa.inspect(op.get_bind()).get_columns(table):
        if col["name"] == column:
            return bool(col["nullable"])
    return False


def _assert_no_duplicate_org_source() -> None:
    """Fail with the offending rows rather than letting Postgres emit an opaque error."""
    rows = op.get_bind().execute(
        sa.text(
            "SELECT source, external_id, count(*) AS n FROM organizations "
            "WHERE external_id IS NOT NULL "
            "GROUP BY source, external_id HAVING count(*) > 1 ORDER BY n DESC LIMIT 20"
        )
    ).fetchall()
    if not rows:
        return
    listed = ", ".join(f"({r[0]!r}, {r[1]!r}) x{r[2]}" for r in rows)
    raise RuntimeError(
        "Cannot add uq_org_source_external_id: organizations already holds duplicate "
        f"(source, external_id) pairs: {listed}. These can only exist on a database that "
        "ran without the constraint. Deduplicate them (keep the row referenced by "
        "organization_members / organization_clusters) and re-run the migration."
    )


def upgrade() -> None:
    if not _has_table("user_preferences"):
        op.create_table(
            "user_preferences",
            sa.Column("user_id", sa.String(255), primary_key=True),
            sa.Column("user_email", sa.String(255), nullable=True),
            sa.Column("settings", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        )
    if not _has_index("user_preferences", "ix_user_preferences_user_id"):
        op.create_index("ix_user_preferences_user_id", "user_preferences", ["user_id"])
    if not _has_index("user_preferences", "ix_user_preferences_email"):
        op.create_index("ix_user_preferences_email", "user_preferences", ["user_email"])

    # The backfill has to precede the ALTER, or SET NOT NULL fails on any legacy row.
    if _is_nullable("organizations", "is_active"):
        op.execute("UPDATE organizations SET is_active = true WHERE is_active IS NULL")
        op.alter_column(
            "organizations",
            "is_active",
            existing_type=sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        )
    if _is_nullable("tenant_access_policies", "is_default"):
        op.execute("UPDATE tenant_access_policies SET is_default = false WHERE is_default IS NULL")
        op.alter_column(
            "tenant_access_policies",
            "is_default",
            existing_type=sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        )

    if not _has_unique_constraint("organizations", "uq_org_source_external_id"):
        _assert_no_duplicate_org_source()
        op.create_unique_constraint(
            "uq_org_source_external_id", "organizations", ["source", "external_id"]
        )
    if not _has_index("organizations", "ix_organizations_source"):
        op.create_index("ix_organizations_source", "organizations", ["source"])
    if not _has_index("tag_assignments", "ix_tag_assignment_tag_id"):
        op.create_index("ix_tag_assignment_tag_id", "tag_assignments", ["tag_id"])


def downgrade() -> None:
    # Every object above except `user_preferences` belongs to an earlier revision
    # (002_org_external_source: the unique constraint and ix_organizations_source;
    # 001_tag_system: ix_tag_assignment_tag_id; 002_rbac_orgs and 004_tenant_acl: the two
    # boolean columns), and upgrade() only creates them where create_all left them out.
    # Dropping them here would make `downgrade base` fail when their owner drops them.
    #
    # important: dropping `user_preferences` discards saved personalization where create_all
    # created the table, the same trade-off as 010_org_cluster_nullable's downgrade.
    if _has_table("user_preferences"):
        if _has_index("user_preferences", "ix_user_preferences_email"):
            op.drop_index("ix_user_preferences_email", table_name="user_preferences")
        if _has_index("user_preferences", "ix_user_preferences_user_id"):
            op.drop_index("ix_user_preferences_user_id", table_name="user_preferences")
        op.drop_table("user_preferences")
