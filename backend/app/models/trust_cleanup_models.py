"""Client certificates this dashboard stopped using, until their LXD trust entries are gone.

Every renew, regenerate, cancel and replaced pending certificate leaves a trust entry on the
cluster that nothing used to remove; one host carried about twenty-five of them. A row here is the
dashboard's own record that it superseded a certificate it issued for one cluster, which is the
only evidence that makes removing that entry safe. Entries this table never saw are not ours to
judge and are never touched.

**One row per (cluster, fingerprint).** The unique constraint, not a read-then-write, is what stops
two workers recording the same certificate twice.

**`due_at` is when to try, not when it was superseded.** The sweep pushes it forward while the
cluster does not yet trust the current certificate, so the grace period starts once removal is
safe rather than when the record was written.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from app.core.database import Base

TRUST_CLEANUP_STATES = ("pending", "failed", "removed", "cancelled")
TRUST_CLEANUP_REASONS = (
    "renew",
    "verify",
    "regenerate",
    "register",
    "cancel",
    "pending_replaced",
    "pending_expired",
    "renew_aborted",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class LxdTrustCleanup(Base):
    __tablename__ = "lxd_trust_cleanup"

    # note: Integer on SQLite only, where nothing but INTEGER PRIMARY KEY autoincrements; the
    # hermetic tests run these rows there. PostgreSQL gets the BIGINT the migration creates.
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    cluster_id = Column(String(255), nullable=False, index=True)
    fingerprint = Column(String(64), nullable=False, comment="LXD form: lowercase sha256 of the DER")
    # note: the column comment stays as revision 039 created it. `register` was added to the
    # reasons after 039 was applied, and an applied revision is never edited; the column is free
    # text, so the comment is documentation and not a constraint.
    reason = Column(
        String(24),
        nullable=False,
        comment="renew | verify | regenerate | cancel | pending_replaced | pending_expired | renew_aborted",
    )
    state = Column(
        String(16), nullable=False, server_default="pending", comment=" | ".join(TRUST_CLEANUP_STATES)
    )
    identity_name = Column(String(255), nullable=True, comment="trust entry name, when known")
    requested_by = Column(String(255), nullable=True, comment="actor whose action superseded it")

    superseded_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    due_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    attempts = Column(Integer, nullable=False, server_default="0")
    last_error = Column(Text, nullable=True)
    last_attempt_at = Column(DateTime(timezone=True), nullable=True)
    removed_at = Column(DateTime(timezone=True), nullable=True)
    already_gone = Column(Boolean, nullable=False, server_default="false")

    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        UniqueConstraint("cluster_id", "fingerprint", name="uq_lxd_trust_cleanup_cluster_fp"),
        Index("ix_lxd_trust_cleanup_state_due", "state", "due_at"),
        CheckConstraint(
            "state IN ('pending', 'failed', 'removed', 'cancelled')",
            name="ck_lxd_trust_cleanup_state",
        ),
    )
