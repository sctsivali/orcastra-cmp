"""Every read the task routers and the feed make, over the ORM tables, with the scope handed in.

Rows come back as mappings keyed by column name (so `metadata`, not `metadata_`), the same
shape the tailer's raw SQL yields, so `present.task_row` reads one shape everywhere. Nothing
here names a scope helper: the caller passes the WHERE terms `app.core.task_scope` built and
judges every row again after the load.
"""

from typing import Any, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import case, func, literal_column, or_, select
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


def _not_housekeeping() -> Any:
    """Every row except LXD's own background work, for the dock's two counts.

    LXD runs tasks nobody asked for ("Updating images", "Cleaning up expired backups"), and the
    follower records them as an LXD task operation with a system actor. A sick daemon can hold
    them open for weeks and swamp the counts. They are still listed by the Tasks and Queue tabs;
    only the counts skip them.

    Spelled as an OR of the ways a row is not housekeeping rather than NOT(a AND b AND c):
    operation_class is nullable, and NOT(TRUE AND NULL AND TRUE) is NULL, which would drop an
    LXD frame that carried no class from the count.
    """
    return or_(
        TaskJournal.kind != "lxd_operation",
        TaskJournal.actor_kind != "system",
        TaskJournal.operation_class.is_(None),
        TaskJournal.operation_class != "task",
    )


async def count_running(db: AsyncSession, conditions: Sequence[Any]) -> int:
    return await _count(
        db,
        TaskJournal.__table__,
        [*conditions, TaskJournal.status.in_(OPEN_STATUSES), _not_housekeeping()],
    )


async def count_failed_recent(
    db: AsyncSession, conditions: Sequence[Any], minutes: int = FAILED_RECENT_MINUTES
) -> int:
    # The window uses the database clock, which is the clock that stamped finished_at.
    cutoff = literal_column(f"now() - interval '{int(minutes)} minutes'")
    return await _count(
        db,
        TaskJournal.__table__,
        [
            *conditions,
            TaskJournal.status == "failure",
            TaskJournal.finished_at >= cutoff,
            _not_housekeeping(),
        ],
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


async def queue_rows(
    db: AsyncSession, conditions: Sequence[Any], limit: int = 100, offset: int = 0
) -> Tuple[List[Mapping[str, Any]], int]:
    """One bounded page in queue order, with pending ranks over the scoped queue.

    Window evaluation precedes LIMIT, so a later page keeps each pending row's rank. Running
    rows have their own partition and never consume a pending position.
    """
    where = [*conditions, TaskJournal.status.in_(OPEN_STATUSES)]
    total = await _count(db, TaskJournal.__table__, where)
    if limit <= 0 or offset >= total:
        return [], total
    position = case(
        (
            TaskJournal.status == "pending",
            func.row_number().over(
                partition_by=(TaskJournal.cluster_id, TaskJournal.status),
                order_by=(TaskJournal.created_at.asc(), TaskJournal.seq.asc()),
            ),
        ),
        else_=None,
    ).label("queue_position")
    stmt = (
        _task_select().add_columns(position)
        .where(*where)
        .order_by(TaskJournal.cluster_id.asc(), TaskJournal.created_at.asc(), TaskJournal.seq.asc())
        .limit(limit)
        .offset(offset)
    )
    return list((await db.execute(stmt)).mappings().all()), total


async def due_schedules(
    db: AsyncSession,
    allowed: Optional[Sequence[str]],
    hours: int = 1,
    *,
    conditions: Sequence[Any] = (),
    limit: int = 100,
) -> Tuple[List[Mapping[str, Any]], int]:
    """Active snapshot schedules due within `hours`, bounded to `allowed` clusters (None = all).

    Empty when the schedule surface is switched off, so the queue never names a feature the
    console does not expose. The caller supplies the project and snapshot ACL predicates;
    they apply to both the count and the bounded page before any rows are loaded.
    """
    if not get_settings().snapshot_schedules_enabled:
        return [], 0
    horizon = literal_column(f"now() + interval '{int(hours)} hours'")
    where = [*conditions, SnapshotSchedule.state == "active", SnapshotSchedule.next_run_at <= horizon]
    if allowed is not None:
        where.append(SnapshotSchedule.cluster_id.in_(list(allowed) or ["__none__"]))
    total = await _count(db, SnapshotSchedule.__table__, where)
    if limit <= 0 or total == 0:
        return [], total
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
        .where(*where)
        .order_by(SnapshotSchedule.next_run_at.asc(), SnapshotSchedule.id.asc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).mappings().all()), total


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
