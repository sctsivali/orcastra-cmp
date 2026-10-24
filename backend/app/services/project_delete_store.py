"""The rows behind durable project-delete follow-ups (app/models/project_delete_models.py).

Every function opens its own short session, so a row written here commits independently of the
caller's transaction. That matters because the cluster lock is transaction scoped: the intent row
must be durable before the DELETE goes out, while the lock is still held by the caller's session.
"""

from __future__ import annotations

import logging
import os
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from secrets import token_hex
from typing import Any, Callable, Collection, FrozenSet, List, Optional

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError

from app.models.project_delete_models import OPEN_STATES, ProjectDeleteFollowup

logger = logging.getLogger(__name__)

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{token_hex(4)}"
# A DELETE gets 3 + 15 s and an operation 10 s, then a 3 + 5 s readback (services/lxd/project_delete.py);
# a lease past that is one no live request still holds.
LEASE = timedelta(seconds=60)
FOLLOW_WINDOW = timedelta(minutes=10)
FIRST_CHECK = timedelta(seconds=5)
MAX_OPEN_PER_CLUSTER = 4
MAX_OPEN = 64
KEEP_FINISHED = timedelta(days=7)

# Tests point this at SQLite. None means the application's own session maker.
session_maker: Optional[Callable[[], Any]] = None


def _maker() -> Callable[[], Any]:
    if session_maker is not None:
        return session_maker
    from app.core.database import async_session_maker

    return async_session_maker


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    # note: SQLite hands timestamps back without a zone; every one stored here is UTC.
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


@dataclass(frozen=True)
class Followup:
    id: Optional[int]
    cluster_id: str
    project_name: str
    origin: str
    requester: Optional[str]
    scope_project: Optional[str]
    event_id: str
    rule_ids: FrozenSet[int]
    state: str
    deadline_at: Optional[datetime] = None
    attempts: int = 0
    # True when the call that got this snapshot opened the row; False when it joined an open one.
    created_here: bool = False


def _snapshot(row: ProjectDeleteFollowup, created_here: bool = False) -> Followup:
    return Followup(
        id=row.id,
        cluster_id=row.cluster_id,
        project_name=row.project_name,
        origin=row.origin,
        requester=row.requester,
        scope_project=row.scope_project,
        event_id=row.event_id,
        rule_ids=frozenset(int(i) for i in (row.rule_ids or [])),
        state=row.state,
        deadline_at=_aware(row.deadline_at),
        attempts=int(row.attempts or 0),
        created_here=created_here,
    )


def unsaved(
    host: str, name: str, *, origin: str, requester: Optional[str], scope_project: Optional[str]
) -> Followup:
    """The announcement of a delete that has no row (the caps were reached when it started)."""
    return Followup(
        id=None, cluster_id=host, project_name=name, origin=origin, requester=requester,
        scope_project=scope_project, event_id=uuid.uuid4().hex, rule_ids=frozenset(), state="settled",
    )


def _open(host: str, name: str):
    return and_(
        ProjectDeleteFollowup.cluster_id == host,
        ProjectDeleteFollowup.project_name == name,
        ProjectDeleteFollowup.state.in_(OPEN_STATES),
    )


async def open_intent(
    host: str,
    name: str,
    *,
    rule_ids: Collection[int],
    origin: str,
    requester: Optional[str],
    scope_project: Optional[str],
) -> Optional[Followup]:
    """Record a delete about to be sent; join the open row for this project if there is one.

    important: a joining call stores none of its own captured rules here. They are merged only if
    its delete ends unknown (mark_pending). A 404 then revokes exactly what earlier deletes
    captured, never the rules staged since for a later project of the same name.

    None when the caps are reached: the delete still runs, without a durable follow-up.
    """
    async with _maker()() as db:
        for _attempt in range(2):
            now = _now()
            row = (
                await db.execute(select(ProjectDeleteFollowup).where(_open(host, name)).with_for_update())
            ).scalar_one_or_none()
            if row is not None:
                row.locked_by = WORKER_ID
                row.lease_until = now + LEASE
                await db.commit()
                return _snapshot(row)
            open_rows = ProjectDeleteFollowup.state.in_(OPEN_STATES)
            total = await db.scalar(select(func.count()).select_from(ProjectDeleteFollowup).where(open_rows))
            on_cluster = await db.scalar(
                select(func.count())
                .select_from(ProjectDeleteFollowup)
                .where(open_rows, ProjectDeleteFollowup.cluster_id == host)
            )
            if (total or 0) >= MAX_OPEN or (on_cluster or 0) >= MAX_OPEN_PER_CLUSTER:
                logger.warning(
                    "Project %s on %s is deleted without a durable follow-up: %s open, %s on its cluster",
                    name, host, total, on_cluster,
                )
                return None
            row = ProjectDeleteFollowup(
                cluster_id=host,
                project_name=name,
                origin=origin,
                requester=requester,
                scope_project=scope_project,
                event_id=uuid.uuid4().hex,
                rule_ids=sorted(int(i) for i in rule_ids),
                state="sending",
                locked_by=WORKER_ID,
                lease_until=now + LEASE,
                next_check_at=now + LEASE,
                deadline_at=now + FOLLOW_WINDOW,
            )
            db.add(row)
            try:
                await db.commit()
            except IntegrityError:
                # Another worker opened the row for this project between the read and the insert.
                await db.rollback()
                continue
            return _snapshot(row, created_here=True)
    return None


async def _update(followup_id: Optional[int], change: Callable[[ProjectDeleteFollowup, datetime], bool]) -> None:
    if followup_id is None:
        return
    async with _maker()() as db:
        row = await db.get(ProjectDeleteFollowup, followup_id)
        if row is None:
            return
        if change(row, _now()) is False:
            await db.delete(row)
        await db.commit()


async def mark_pending(followup: Followup, *, merge_ids: Collection[int]) -> None:
    """The delete's outcome is unknown: the sweeper owns it from here."""

    def change(row: ProjectDeleteFollowup, now: datetime) -> bool:
        row.rule_ids = sorted({int(i) for i in (row.rule_ids or [])} | {int(i) for i in merge_ids})
        row.state = "pending"
        row.locked_by = None
        row.lease_until = None
        row.next_check_at = now + FIRST_CHECK
        return True

    await _update(followup.id, change)


async def give_back(followup: Followup) -> None:
    """The delete did not happen: drop a row this call opened, release one it joined."""

    def change(row: ProjectDeleteFollowup, now: datetime) -> bool:
        if followup.created_here:
            return False
        row.locked_by = None
        row.lease_until = None
        return True

    await _update(followup.id, change)


async def mark_settled(followup_id: Optional[int]) -> None:
    """The project is gone and its rules were revoked; only the announcement is still owed.

    important: the caller keeps the lease while it announces. Released here, another worker's
    sweep claimed the row in the same instant and announced it a second time (seen live: two
    audit lines 150 ms apart). If the caller dies first, the lease runs out and a sweep announces.
    """

    def change(row: ProjectDeleteFollowup, now: datetime) -> bool:
        row.state = "settled"
        row.settled_at = now
        row.locked_by = WORKER_ID
        row.lease_until = now + LEASE
        row.next_check_at = now + LEASE
        row.attempts = 0
        return True

    await _update(followup_id, change)


async def mark_done(followup_id: Optional[int], error: Optional[str] = None) -> None:
    def change(row: ProjectDeleteFollowup, now: datetime) -> bool:
        row.state = "done"
        row.locked_by = None
        row.lease_until = None
        row.last_error = error
        return True

    await _update(followup_id, change)


async def expire(followup_id: Optional[int], reason: str) -> None:
    def change(row: ProjectDeleteFollowup, now: datetime) -> bool:
        row.state = "expired"
        row.locked_by = None
        row.lease_until = None
        row.last_error = reason
        return True

    await _update(followup_id, change)


async def retry_later(followup_id: Optional[int], delay: timedelta, error: Optional[str] = None) -> None:
    def change(row: ProjectDeleteFollowup, now: datetime) -> bool:
        row.locked_by = None
        row.lease_until = None
        row.next_check_at = now + delay
        if error is not None:
            row.last_error = error
        return True

    await _update(followup_id, change)


async def claim_due(limit: int) -> List[Followup]:
    """Lease up to `limit` rows that are due, at most one per cluster, for this worker.

    A `sending` row is due only once its request's lease ran out: that request is gone (its worker
    crashed or restarted between sending the DELETE and reading the answer), and nobody else will
    settle it. Claimed, it becomes `pending`, so a later pass can take it again.
    """
    now = _now()
    unleased = or_(ProjectDeleteFollowup.lease_until.is_(None), ProjectDeleteFollowup.lease_until < now)
    due = and_(
        ProjectDeleteFollowup.state.in_(("sending", "pending", "settled")),
        ProjectDeleteFollowup.next_check_at <= now,
        unleased,
    )
    async with _maker()() as db:
        rows = (
            await db.execute(
                select(ProjectDeleteFollowup)
                .where(due)
                .order_by(ProjectDeleteFollowup.next_check_at)
                .limit(limit * 4)
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        picked: List[ProjectDeleteFollowup] = []
        clusters = set()
        for row in rows:
            if row.cluster_id in clusters:
                continue
            clusters.add(row.cluster_id)
            if row.state == "sending":
                row.state = "pending"
            row.locked_by = WORKER_ID
            row.lease_until = now + LEASE
            row.attempts = int(row.attempts or 0) + 1
            picked.append(row)
            if len(picked) >= limit:
                break
        await db.commit()
        return [_snapshot(row) for row in picked]


async def prune() -> int:
    cutoff = _now() - KEEP_FINISHED
    async with _maker()() as db:
        result = await db.execute(
            delete(ProjectDeleteFollowup).where(
                ProjectDeleteFollowup.state.in_(("done", "expired")),
                ProjectDeleteFollowup.updated_at < cutoff,
            )
        )
        await db.commit()
        return int(result.rowcount or 0)
