"""A registry of the keys My Keys issued, kept after a key is deleted (services/my_keys_registry.py).

Vault holds the live keys and is their source of truth. This table serves the audit trail: it
keeps a deleted key's owner and cluster, so its owner can still read its history, and maps the
certificate fingerprint LXD reports as the requestor back to the key, so what was done on the
cluster with a key appears in that key's activity.
"""

from sqlalchemy import Column, DateTime, Index, String

from app.core.database import Base


class MyKeyRecord(Base):
    __tablename__ = "my_key_records"

    key_id = Column(String(255), primary_key=True)
    # The certificate's SHA-256, lowercase hex: what LXD names as the requestor of a TLS identity.
    fingerprint = Column(String(64), nullable=False, unique=True)
    owner_email = Column(String(255), nullable=True)
    cluster_id = Column(String(255), nullable=False)
    project = Column(String(255), nullable=True)
    name = Column(String(255), nullable=True)
    auth_level = Column(String(32), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    deleted_by = Column(String(255), nullable=True)
    # The last change LXD reported under this certificate, kept past the journal's own retention.
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    last_used_action = Column(String(96), nullable=True)
    last_used_cluster = Column(String(255), nullable=True)

    __table_args__ = (
        Index("ix_my_key_records_owner_email", "owner_email"),
        Index("ix_my_key_records_cluster_id", "cluster_id"),
    )
