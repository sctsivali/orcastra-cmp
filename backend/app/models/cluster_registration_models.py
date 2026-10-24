"""Which registration of a cluster id is the live one, so a key grant can be bound to it.

A cluster id is free text, and once deregistered it can be registered again, possibly by another
organization. A grant that matched by id alone would then reach the new cluster. Each registration
gets a `registration_id`; a grant stores the one it was given for, and matches only while that
registration is active (services/cluster_generations.py).

**Postgres, not the Vault record.** Deregistration deletes the Vault record best effort, and a
registration merges over whatever record is left, so an id stored there could outlive its cluster.
The generation is ended here, in a transaction the deregistration must commit before it deletes
anything.

**`legacy`** marks a registration adopted from before generations existed. Grants written before
then carry no stamp, and match only a legacy registration; once that one ends, they match nothing.
"""

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, Column, DateTime, Index, Integer, String, text

from app.core.database import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ClusterRegistration(Base):
    __tablename__ = "cluster_registrations"

    # note: Integer on SQLite only, where nothing but INTEGER PRIMARY KEY autoincrements; the
    # hermetic tests run these rows there. PostgreSQL gets the BIGINT the migration creates.
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    cluster_id = Column(String(255), nullable=False, index=True)
    registration_id = Column(String(64), nullable=False, unique=True)
    legacy = Column(Boolean, nullable=False, server_default="false", default=False)
    registered_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    ended_reason = Column(String(32), nullable=True, comment="deregistered | replaced")

    __table_args__ = (
        Index(
            "uq_cluster_registration_active",
            "cluster_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
    )


class ClusterRegistrationAdoption(Base):
    """One row once every cluster registered before generations has a legacy registration.

    important: while it is missing, an id with no registration row is treated as it was before
    generations (its unstamped grants still match), and only the startup backfill may adopt one.
    Once it exists, adoption is closed for good: an id with no active row matches no grant.
    """

    __tablename__ = "cluster_registration_adoption"

    id = Column(Integer, primary_key=True)
    closed_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    adopted = Column(Integer, nullable=False, server_default="0")
