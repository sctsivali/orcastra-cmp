"""Widen session audit byte and duration counters to 64-bit.

`session_audit_logs.download_bytes` and `.upload_bytes` were 32-bit integers, so a
session moving more than 2 GiB overflowed on INSERT. That failure is invisible: the
disconnect row is written fire-and-forget with a short retry
(`app/services/session_audit.py`), so the retries fail the same way and the session
simply has no disconnect record - no duration, no totals, and a connect row that
looks like a session that never ended. A SPICE console shifting 2 GiB is an ordinary
afternoon, not an edge case.

`duration_seconds` is widened alongside them. It is the same column family and the
same class of silent truncation, and doing it now avoids a second migration later.

Widening integer to bigint in PostgreSQL rewrites the column in place under an ACCESS
EXCLUSIVE lock. The table only ever receives two small rows per session, so it is
small and the lock is brief.
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
    # guarded the same way as 014: this has to be a no-op on a database that already
    # has the wider type, rather than an error.
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
    # note: narrowing is lossy. Any row already holding a value above 2^31-1 would
    # fail the cast, which is the correct outcome - it refuses rather than silently
    # corrupting an audit record.
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
