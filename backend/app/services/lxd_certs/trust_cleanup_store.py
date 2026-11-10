"""Rows of `lxd_trust_cleanup`, and the per-cluster lock every certificate change takes.

important: without the lock, two renewals racing on one cluster each read the same current
certificate, mint a new one and add it to trust, and only the last write to Vault survives, so the
loser's certificate stays trusted on the cluster with nothing naming it. Serializing every
certificate mutation of a cluster (renew, verify, cancel, regenerate, remove now, the sweep,
deregistration) prevents that and makes the select-then-insert below safe.
"""

import asyncio
import hashlib
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Callable, Dict, Iterable, List, Optional, Set

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cert_fingerprint import is_lxd_fingerprint
from app.models.trust_cleanup_models import LxdTrustCleanup

# A removed row stays visible this long: the page can say "removed 3 minutes ago", and the task
# journal still recognises an operation that started under the previous certificate and ended after.
RECENTLY_REMOVED = timedelta(hours=24)

OPEN_STATES = ("pending", "failed")

_local_locks: Dict[tuple, asyncio.Lock] = {}
_local_locks_guard = threading.Lock()


class CertificateOperationBusy(Exception):
    """Another certificate change on this cluster holds the lock."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _advisory_key(cluster_id: str) -> int:
    digest = hashlib.sha256(f"orcastra:cluster-cert:{cluster_id}".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _dialect_name(db: AsyncSession) -> str:
    try:
        bind = db.get_bind()
        return str(bind.dialect.name).lower() if bind is not None else ""
    except (AttributeError, TypeError):
        return ""


def _local_lock(cluster_id: str) -> asyncio.Lock:
    # SQLite and test sessions only. An asyncio lock belongs to one loop, so it is keyed by the loop.
    key = (id(asyncio.get_running_loop()), cluster_id)
    with _local_locks_guard:
        return _local_locks.setdefault(key, asyncio.Lock())


@asynccontextmanager
async def cluster_cert_lock(
    db: AsyncSession, cluster_id: str, *, wait: bool = False
) -> AsyncIterator[None]:
    """Hold this cluster's certificate still until the caller commits or rolls back.

    PostgreSQL's transaction advisory lock serializes every worker and host and is released with
    the transaction, so a worker that dies mid-change frees it. ``wait=False`` raises
    CertificateOperationBusy instead of queueing, which a request turns into 409 and the sweep
    into "next pass".
    """
    if _dialect_name(db) == "postgresql":
        if wait:
            await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _advisory_key(cluster_id)})
        else:
            got = await db.execute(
                text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": _advisory_key(cluster_id)}
            )
            if not bool(got.scalar()):
                raise CertificateOperationBusy(cluster_id)
        yield
        return
    lock = _local_lock(cluster_id)
    if not wait and lock.locked():
        raise CertificateOperationBusy(cluster_id)
    async with lock:
        yield


async def record_superseded(
    db: AsyncSession,
    *,
    cluster_id: str,
    fingerprint: str,
    reason: str,
    grace_seconds: int,
    identity_name: Optional[str] = None,
    requested_by: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[LxdTrustCleanup]:
    """Write down that this dashboard stopped using `fingerprint` on `cluster_id`.

    Called under cluster_cert_lock and BEFORE the certificate is overwritten, so a failed overwrite
    leaves a row the sweep cancels (it names the still-current certificate) rather than a trusted
    certificate nothing remembers. Returns None for a malformed fingerprint, which is never stored.
    """
    if not is_lxd_fingerprint(fingerprint):
        return None
    now = now or utcnow()
    due = now + timedelta(seconds=grace_seconds)
    row = (
        await db.execute(
            select(LxdTrustCleanup).where(
                LxdTrustCleanup.cluster_id == cluster_id,
                LxdTrustCleanup.fingerprint == fingerprint,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = LxdTrustCleanup(
            cluster_id=cluster_id,
            fingerprint=fingerprint,
            reason=reason,
            state="pending",
            identity_name=identity_name,
            requested_by=requested_by,
            superseded_at=now,
            due_at=due,
            attempts=0,
            already_gone=False,
        )
        db.add(row)
    elif row.state != "removed":
        # A cancelled or failed record of the same certificate is live again: the dashboard has
        # just let go of it a second time.
        row.state = "pending"
        row.reason = reason
        row.due_at = due
        row.last_error = None
        row.identity_name = identity_name or row.identity_name
        row.requested_by = requested_by or row.requested_by
    await db.flush()
    return row


async def rows_for_clusters(
    db: AsyncSession, cluster_ids: Iterable[str], *, now: Optional[datetime] = None
) -> Dict[str, List[LxdTrustCleanup]]:
    """Open rows plus recently removed ones, grouped by cluster, for the certificate page."""
    wanted = sorted(set(cluster_ids))
    if not wanted:
        return {}
    since = (now or utcnow()) - RECENTLY_REMOVED
    result = await db.execute(
        select(LxdTrustCleanup)
        .where(LxdTrustCleanup.cluster_id.in_(wanted))
        .where(
            LxdTrustCleanup.state.in_(OPEN_STATES)
            | ((LxdTrustCleanup.state == "removed") & (LxdTrustCleanup.removed_at >= since))
        )
        .order_by(LxdTrustCleanup.superseded_at.desc())
    )
    grouped: Dict[str, List[LxdTrustCleanup]] = {}
    for row in result.scalars().all():
        grouped.setdefault(row.cluster_id, []).append(row)
    return grouped


async def open_rows(
    db: AsyncSession,
    cluster_id: str,
    *,
    due_by: Optional[datetime] = None,
    include_failed: bool = False,
) -> List[LxdTrustCleanup]:
    states = OPEN_STATES if include_failed else ("pending",)
    query = select(LxdTrustCleanup).where(
        LxdTrustCleanup.cluster_id == cluster_id, LxdTrustCleanup.state.in_(states)
    )
    if due_by is not None:
        query = query.where(LxdTrustCleanup.due_at <= due_by)
    result = await db.execute(query.order_by(LxdTrustCleanup.superseded_at))
    return list(result.scalars().all())


async def clusters_with_pending(db: AsyncSession, *, due_by: Optional[datetime] = None) -> List[str]:
    query = select(LxdTrustCleanup.cluster_id).where(LxdTrustCleanup.state == "pending")
    if due_by is not None:
        query = query.where(LxdTrustCleanup.due_at <= due_by)
    result = await db.execute(query.distinct())
    return sorted(cid for cid in result.scalars().all() if cid)


async def tracked_fingerprints(
    db: AsyncSession, cluster_id: str, *, now: Optional[datetime] = None
) -> Set[str]:
    """Superseded fingerprints that may still legitimately be the dashboard on `cluster_id`.

    A row waiting out its grace period (workers that have not switched yet), or one removed within
    the last day (an operation started before the removal can still report after it). Never a
    `failed` row: that certificate can stay trusted indefinitely, and counting it as the dashboard
    would file anything done with it, including by someone who obtained its key, as the dashboard's.
    """
    since = (now or utcnow()) - RECENTLY_REMOVED
    result = await db.execute(
        select(LxdTrustCleanup.fingerprint).where(
            LxdTrustCleanup.cluster_id == cluster_id,
            (LxdTrustCleanup.state == "pending")
            | ((LxdTrustCleanup.state == "removed") & (LxdTrustCleanup.removed_at >= since)),
        )
    )
    return set(result.scalars().all())


async def delete_for_cluster(db: AsyncSession, cluster_id: str) -> int:
    result = await db.execute(delete(LxdTrustCleanup).where(LxdTrustCleanup.cluster_id == cluster_id))
    return int(result.rowcount or 0)


async def record_superseded_now(session_maker: Callable[[], Any], **record: Any) -> None:
    """record_superseded on its own short transaction, committed before the overwrite it protects.

    important: the caller's session holds the cluster lock, and that lock is transaction scoped, so
    committing there would release it mid-change. Committing the row only after the Vault
    overwrite leaves a window where a dead worker or a failed commit loses the row while the
    overwrite stays, and the replaced certificate stays trusted with nothing naming it. A row
    committed first costs nothing if the overwrite then fails: the sweep cancels a row that still
    names the current certificate.
    """
    async with session_maker() as db:
        await record_superseded(db, **record)
        await db.commit()
