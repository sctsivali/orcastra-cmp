"""The Mini edition's own tables: operator identities, their certificates, and the audit chain.

An operator signs in with a TLS client certificate. The identity carries the role, the unique
email every email-keyed table (organization membership, keys, schedules) knows them by, and a
stable UUID that sessions and Migration Center pin; each certificate it holds is a separate row,
so a renewal or a second device adds a certificate without changing who the operator is.

important: an identity is never deleted. Deleting one tombstones it (status ``deleted``), which
keeps its email reserved: a new person given the same address would otherwise inherit the old
one's memberships and grants.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, Column, DateTime, ForeignKey, Index, Integer, SmallInteger, String, text,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.core.database import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


IDENTITY_ROLES = ("admin", "partner", "tenant")
IDENTITY_STATUSES = ("active", "revoked", "deleted")
CERTIFICATE_STATUSES = ("active", "revoked", "superseded")


class LocalIdentity(Base):
    __tablename__ = "local_identities"

    id = Column(Integer, primary_key=True, autoincrement=True)
    uuid = Column(String(36), nullable=False, unique=True)
    email = Column(String(320), nullable=False)
    common_name = Column(String(255), nullable=False)
    display_name = Column(String(255), nullable=True)
    role = Column(String(20), nullable=False, default="tenant")
    status = Column(String(20), nullable=False, default="active", index=True)
    # The organization a Partner issued this Tenant for; a Partner manages only identities whose
    # owner organization it holds. Null for identities an Administrator issued.
    owner_org_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True)
    issued_by = Column(String(320), nullable=True)
    # "bootstrap" for the first administrator, else the issuer's email. Kept from the first release.
    created_by = Column(String(255), nullable=True)
    # A session token minted before this instant is refused: a revoke, role change or delete
    # moves it forward, so no older token outlives the change.
    tokens_valid_after = Column(DateTime(timezone=True), nullable=True)
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    # The rank of whoever last revoked or deleted this operator; a lower rank may not undo it.
    status_changed_by_role = Column(String(20), nullable=True)
    # The first release keyed identities on one certificate; that certificate now lives in
    # local_identity_certificates and these columns are kept, unused, for a downgrade.
    fingerprint = Column(String(64), nullable=True, index=True)
    serial = Column(String(128), nullable=True)
    not_after = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)

    __table_args__ = (
        Index("uq_local_identities_email", text("lower(email)"), unique=True),
        Index(
            "uq_local_identities_single_bootstrap_admin", "role", unique=True,
            postgresql_where=text("role = 'admin' AND status = 'active' AND created_by = 'bootstrap'"),
        ),
    )

    def __repr__(self) -> str:
        return f"<LocalIdentity(email={self.email}, role={self.role}, status={self.status})>"


class LocalIdentityCertificate(Base):
    __tablename__ = "local_identity_certificates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    identity_id = Column(Integer, ForeignKey("local_identities.id", ondelete="CASCADE"), nullable=False, index=True)
    fingerprint = Column(String(64), nullable=False, unique=True)  # sha256 hex of the DER certificate
    common_name = Column(String(255), nullable=False)
    serial = Column(String(128), nullable=True)
    not_after = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(20), nullable=False, default="active", index=True)
    # The certificate this one renewed. The older one stays active until this one is first used
    # (or the overlap ends), so a renewal never locks the holder out mid-switch.
    renewed_from_id = Column(Integer, ForeignKey("local_identity_certificates.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)


class AuditLogEntry(Base):
    """One link of the append-only audit chain: row_hash = sha256(prev_hash + canonical(fields))."""

    __tablename__ = "audit_log"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    event_id = Column(String(64), nullable=False, index=True)
    occurred_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    actor_id = Column(String(255), nullable=True, index=True)
    actor_email = Column(String(255), nullable=True)
    actor_role = Column(String(50), nullable=True)
    action = Column(String(120), nullable=False, index=True)
    category = Column(String(60), nullable=True)
    severity = Column(String(20), nullable=True, default="info")
    result = Column(String(20), nullable=True, default="success")
    target_type = Column(String(60), nullable=True)
    target_id = Column(String(255), nullable=True)
    target_host = Column(String(255), nullable=True)
    target_project = Column(String(120), nullable=True)
    request_id = Column(String(64), nullable=True)
    source_ip = Column(String(64), nullable=True)
    details = Column(JSONB, nullable=True)
    prev_hash = Column(String(64), nullable=True)
    row_hash = Column(String(64), nullable=False)
    # Which canonical form row_hash covers, so rows written by the first release still verify
    # if the form ever changes.
    chain_version = Column(SmallInteger, nullable=False, default=1, server_default="1")

    __table_args__ = (
        Index("ix_audit_log_occurred", "occurred_at"),
        Index("ix_audit_log_actor_action", "actor_id", "action"),
    )


class AuditLogAnchor(Base):
    """Single row (id 1): the verified high-water mark, and where retention last cut the chain.

    The hash chain proves rows were not edited or reordered; it cannot show a deleted tail. The
    anchor holds the verified max id, count and head hash, so a shorter chain is caught. Retention
    deletes a prefix and records its last id and hash here, so verification restarts from it.
    """

    __tablename__ = "audit_log_anchor"

    id = Column(Integer, primary_key=True)
    max_id = Column(BigInteger, nullable=False, default=0)
    row_count = Column(BigInteger, nullable=False, default=0)
    head_hash = Column(String(64), nullable=True)
    pruned_through_id = Column(BigInteger, nullable=False, default=0, server_default="0")
    pruned_head_hash = Column(String(64), nullable=True)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now)
