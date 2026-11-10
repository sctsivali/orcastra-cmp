"""Project deletes whose rule revoke and announcement are still owed, so a restart cannot lose them.

A row is written before the DELETE goes to LXD and stays until the outcome is settled: deleted
(the captured rules are revoked and the deletion announced), refused (the row goes), or still
present when its deadline passes (nothing is revoked). Any worker's sweeper can finish a row whose
lease ran out, so a crash between sending the DELETE and reading its answer is safe.

important: only the partial unique index over the open states keeps one open row per (cluster,
project); a read-then-write would let two workers open two. `rule_ids` are the rules captured
under the cluster lock, never the rules naming the project when the row is finished: a rule
staged after the delete landed belongs to a later project.
"""

from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, CheckConstraint, Column, DateTime, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB

from app.core.database import Base

FOLLOWUP_STATES = ("sending", "pending", "settled", "done", "expired")
OPEN_STATES = ("sending", "pending")
FOLLOWUP_ORIGINS = ("route", "proxy")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ProjectDeleteFollowup(Base):
    __tablename__ = "project_delete_followups"

    # Integer on SQLite only, where nothing but INTEGER PRIMARY KEY autoincrements; the
    # hermetic tests run these rows there. PostgreSQL gets the BIGINT the migration creates.
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    cluster_id = Column(String(255), nullable=False, index=True)
    project_name = Column(String(255), nullable=False)
    origin = Column(String(8), nullable=False, comment="route | proxy")
    requester = Column(String(255), nullable=True, comment="user id or API key id that asked")
    scope_project = Column(String(255), nullable=True, comment="the proxy request's project, for its event")
    event_id = Column(String(64), nullable=False, comment="stable id for every announcement of this delete")
    rule_ids = Column(JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=list)
    state = Column(String(12), nullable=False, server_default="sending", comment=" | ".join(FOLLOWUP_STATES))
    locked_by = Column(String(96), nullable=True)
    lease_until = Column(DateTime(timezone=True), nullable=True)
    next_check_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    deadline_at = Column(DateTime(timezone=True), nullable=False)
    attempts = Column(Integer, nullable=False, server_default="0")
    last_error = Column(Text, nullable=True)
    settled_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        Index(
            "uq_project_delete_followup_open",
            "cluster_id",
            "project_name",
            unique=True,
            postgresql_where=text("state IN ('sending', 'pending')"),
        ),
        Index("ix_project_delete_followups_due", "state", "next_check_at"),
        CheckConstraint(
            "state IN ('sending', 'pending', 'settled', 'done', 'expired')",
            name="ck_project_delete_followups_state",
        ),
        CheckConstraint("origin IN ('route', 'proxy')", name="ck_project_delete_followups_origin"),
    )
