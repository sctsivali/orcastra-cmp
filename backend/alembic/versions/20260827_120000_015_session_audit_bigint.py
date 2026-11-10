"""Widen session audit byte and duration counters to 64-bit.

As 32-bit integers, `download_bytes` and `upload_bytes` overflow on INSERT for a
session moving more than 2 GiB, which a SPICE console reaches easily. The disconnect
row is written fire-and-forget with a short retry (`app/services/session_audit.py`),
so the failure is silent: no duration, no totals, and a connect row that looks like a
session that never ended. `duration_seconds` is widened with them.

Widening integer to bigint rewrites the column under an ACCESS EXCLUSIVE lock; the
table gets two small rows per session, so the lock is brief.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "015_session_audit_bigint"
down_revision = "014_schema_convergence"
branch_labels = None
depends_on = None

TABLE = "session_audit_logs"
COLUMNS = ("duration_seconds", "download_bytes", "upload_bytes")


def _column_types() -> dict:
    inspector = sa.inspect(op.get_bind())
    if TABLE not in inspector.get_table_names():
        return {}
    return {c["name"]: c["type"] for c in inspector.get_columns(TABLE)}


def upgrade() -> None:
    # Guarded like 014: a no-op on a database that already has the wider type.
    types = _column_types()
    for name in COLUMNS:
        current = types.get(name)
        if current is None or isinstance(current, sa.BigInteger):
            continue
        op.alter_column(
            TABLE,
            name,
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            existing_nullable=True,
        )


def downgrade() -> None:
    # Narrowing is lossy: a row holding a value above 2^31-1 fails the cast, so the
    # downgrade refuses instead of corrupting an audit record.
    types = _column_types()
    for name in COLUMNS:
        current = types.get(name)
        if current is None or not isinstance(current, sa.BigInteger):
            continue
        op.alter_column(
            TABLE,
            name,
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=True,
        )
