"""Per-cluster records that end with a cluster's registration."""

from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import TagAssignment
from app.models.snapshot_schedule_models import SnapshotSchedule


async def forget_cluster_records(db: AsyncSession, cluster_id: str) -> dict[str, int]:
    """Delete the tag assignments and snapshot schedules naming a deregistered cluster. Caller commits.

    important: only records whose subject was the cluster's instances and projects. Tags themselves
    are shared across clusters and stay; history (logs, the task journal, migration jobs) stays.
    Left behind, a schedule would keep running for a cluster that is gone, and a cluster registered
    again under the same id would show the earlier tags on new instances that reuse a name.
    """
    tags = await db.execute(delete(TagAssignment).where(TagAssignment.resource_host == cluster_id))
    schedules = await db.execute(delete(SnapshotSchedule).where(SnapshotSchedule.cluster_id == cluster_id))
    return {
        "tag_assignments": int(tags.rowcount or 0),
        "snapshot_schedules": int(schedules.rowcount or 0),
    }
