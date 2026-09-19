"""Every read the task routers and the feed make, over the ORM tables, with the scope handed in.

Rows come back as mappings keyed by column name (so `metadata`, not `metadata_`), the same
shape the tailer's raw SQL yields, so `present.task_row` reads one shape everywhere. Nothing
here names a scope helper: the caller passes the WHERE terms `app.core.task_scope` built and
judges every row again after the load.
"""

from typing import Any, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.snapshot_schedule_models import SnapshotSchedule
from app.models.task_journal_models import ClusterLog, TaskJournal
from app.services.task_journal import repository

OPEN_STATUSES = ("pending", "running")
FAILED_RECENT_MINUTES = 15


def _task_select() -> Any:
    return select(TaskJournal.__table__)


def _log_select() -> Any:
    return select(ClusterLog.__table__)


async def _count(db: AsyncSession, table: Any, conditions: Sequence[Any]) -> int:
    stmt = select(func.count()).select_from(table).where(*conditions)
    return int((await db.execute(stmt)).scalar() or 0)


async def list_tasks(
    db: AsyncSession,
    conditions: Sequence[Any],
    filters: Sequence[Any],
    limit: int,
    offset: int,
) -> Tuple[List[Mapping[str, Any]], int]:
    """One page of tasks newest change first, and the total over the same terms.

    A limit of 0 answers the counts alone, which is what the collapsed strip polls for.
    """
    where = [*conditions, *filters]
    total = await _count(db, TaskJournal.__table__, where)
    if limit <= 0 or offset >= total:
        return [], total
    stmt = (
        _task_select()
        .where(*where)
        .order_by(TaskJournal.updated_at.desc(), TaskJournal.seq.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await db.execute(stmt)).mappings().all()
    return list(rows), total


async def count_running(db: AsyncSession, conditions: Sequence[Any]) -> int:
    return await _count(
        db, TaskJournal.__table__, [*conditions, TaskJournal.status.in_(OPEN_STATUSES)]
    )


async def count_failed_recent(
    db: AsyncSession, conditions: Sequence[Any], minutes: int = FAILED_RECENT_MINUTES
) -> int:
    # note: the window is the database clock, which is the clock that stamped finished_at.
    cutoff = literal_column(f"now() - interval '{int(minutes)} minutes'")
    return await _count(
        db,
        TaskJournal.__table__,
        [*conditions, TaskJournal.status == "failure", TaskJournal.finished_at >= cutoff],
    )


async def get_task(db: AsyncSession, task_id: str) -> Optional[Mapping[str, Any]]:
    stmt = _task_select().where(TaskJournal.id == task_id)
    return (await db.execute(stmt)).mappings().first()


async def task_log(db: AsyncSession, task_id: str, limit: int = 500) -> List[Mapping[str, Any]]:
    stmt = (
        _log_select()
        .where(ClusterLog.task_id == task_id)
        .order_by(ClusterLog.ts.asc(), ClusterLog.id.asc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).mappings().all())


async def queue_rows(db: AsyncSession, conditions: Sequence[Any]) -> List[Mapping[str, Any]]:
    """Every open row in scope, oldest first per cluster, which is the queue position."""
    stmt = (
        _task_select()
        .where(*conditions, TaskJournal.status.in_(OPEN_STATUSES))
        .order_by(TaskJournal.cluster_id.asc(), TaskJournal.created_at.asc(), TaskJournal.seq.asc())
    )
    return list((await db.execute(stmt)).mappings().all())


async def due_schedules(
    db: AsyncSession, allowed: Optional[Sequence[str]], hours: int = 1
) -> List[Mapping[str, Any]]:
    """Active snapshot schedules due within `hours`, bounded to `allowed` clusters (None = all).

    Empty when the schedule surface is switched off, so the queue never names a feature the
    console does not expose. The caller applies the per-row project and permission checks.
    """
    if not get_settings().snapshot_schedules_enabled:
        return []
    horizon = literal_column(f"now() + interval '{int(hours)} hours'")
    stmt = (
        select(
            SnapshotSchedule.id.label("schedule_id"),
            SnapshotSchedule.cluster_id,
            SnapshotSchedule.project,
            SnapshotSchedule.instance_name,
            SnapshotSchedule.label,
            SnapshotSchedule.next_run_at,
            SnapshotSchedule.run_state,
        )
        .where(SnapshotSchedule.state == "active", SnapshotSchedule.next_run_at <= horizon)
        .order_by(SnapshotSchedule.next_run_at.asc(), SnapshotSchedule.id.asc())
    )
    if allowed is not None:
        stmt = stmt.where(SnapshotSchedule.cluster_id.in_(list(allowed) or ["__none__"]))
    return list((await db.execute(stmt)).mappings().all())


async def list_log(
    db: AsyncSession,
    conditions: Sequence[Any],
    filters: Sequence[Any],
    limit: int,
    offset: int,
) -> Tuple[List[Mapping[str, Any]], int]:
    """One page of log lines newest first, and the total over the same terms."""
    where = [*conditions, *filters]
    total = await _count(db, ClusterLog.__table__, where)
    if limit <= 0 or offset >= total:
        return [], total
    stmt = (
        _log_select()
        .where(*where)
        .order_by(ClusterLog.ts.desc(), ClusterLog.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list((await db.execute(stmt)).mappings().all()), total


async def follower_snapshot(db: AsyncSession) -> List[Mapping[str, Any]]:
    return await repository.read_cluster_states(db)


async def follower_active(db: AsyncSession) -> bool:
    return await repository.follower_is_active(db)
