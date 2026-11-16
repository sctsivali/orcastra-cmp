"""Whether a rebuild may start now: one in flight per instance, and a Tenant's budget and cap.

A rebuild force-stops the guest, pulls an image and rewrites the whole root disk, so on a shared
host it is the heaviest thing a customer can start. The limits follow how public clouds guard a
shared host: one operation in flight per resource for everyone; for a Tenant, a rolling budget per
instance and a cap on how many run at once across its organization's projects on the cluster.

Every count reads `rebuild_jobs`, the record each rebuild writes, so a limit survives a Redis outage
and a restart. The caller holds `guard_instance_mutation` (a per-cluster advisory lock) around this
and the insert that follows, so two requests cannot both pass a count and both start.

Time windows are compared in Python: the rows involved are few (in-flight ones, or the last few for
one instance), and it keeps naive and aware timestamps from comparing as text.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from fastapi import HTTPException
from sqlalchemy import select

from app.models.db_models import RebuildJob

IN_FLIGHT_STATUSES = ("pending", "stopping", "rebuilding", "starting", "provisioning")
# The worker driving a rebuild refreshes its row every HEARTBEAT while LXD works, however long the
# image pull takes (routers/instance_rebuild._heartbeat). A row nobody refreshed for STRANDED_AFTER
# has no worker behind it: it blocks nothing, and recover_stranded_rebuilds fails it.
HEARTBEAT = timedelta(seconds=30)
STRANDED_AFTER = timedelta(minutes=5)
# important: a row whose worker died in `rebuilding` may name a daemon rebuild LXD accepted and is
# still running (the advisory lock died with the worker's connection; LXD's wait is up to 1800 s,
# services/lxd/instance.rebuild_instance). It keeps blocking a second rebuild and Migration Center
# (migrations/planning.py) until that wait is over.
REBUILD_OP_WINDOW = timedelta(seconds=1800) + STRANDED_AFTER
BUDGET_WINDOW = timedelta(hours=1)


def _aware(moment: Optional[datetime]) -> Optional[datetime]:
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def silent_window(status: Optional[str]) -> timedelta:
    """How long a row in `status` may go unrefreshed before no worker or daemon can be behind it."""
    return REBUILD_OP_WINDOW if status == "rebuilding" else STRANDED_AFTER


def _live(row: RebuildJob, now: datetime) -> bool:
    updated = _aware(row.updated_at) or _aware(row.created_at)
    return updated is not None and now - updated < silent_window(row.status)


def plain_wait(seconds: int) -> str:
    minutes = max(1, math.ceil(seconds / 60))
    return f"{minutes} minute" if minutes == 1 else f"{minutes} minutes"


def _turned_off() -> HTTPException:
    return HTTPException(
        status_code=403,
        detail={
            "error": "rebuild_disabled",
            "message": "Rebuild is turned off for tenants on this deployment. Ask your provider to rebuild it.",
        },
    )


async def _running(session, host: str, project: str, instance_name: str, now: datetime) -> Optional[RebuildJob]:
    rows = (
        await session.execute(
            select(RebuildJob).where(
                RebuildJob.cluster_id == host,
                RebuildJob.project == project,
                RebuildJob.instance_name == instance_name,
                RebuildJob.status.in_(IN_FLIGHT_STATUSES),
            )
        )
    ).scalars().all()
    return next((row for row in rows if _live(row, now)), None)


async def _budget_wait(session, host: str, project: str, instance_name: str, per_hour: int, now: datetime) -> int:
    """Seconds until another rebuild of this instance fits the hourly budget; 0 when it fits now."""
    recent = (
        await session.execute(
            select(RebuildJob.created_at)
            .where(
                RebuildJob.cluster_id == host,
                RebuildJob.project == project,
                RebuildJob.instance_name == instance_name,
            )
            .order_by(RebuildJob.created_at.desc())
            .limit(per_hour)
        )
    ).scalars().all()
    if len(recent) < per_hour:
        return 0
    oldest = _aware(recent[-1])
    frees_at = oldest + BUDGET_WINDOW
    return max(0, math.ceil((frees_at - now).total_seconds()))


async def _running_in(session, host: str, projects: Optional[Iterable[str]], now: datetime) -> int:
    """In-flight rebuilds on `host` in `projects`; None counts every project on the cluster."""
    query = select(RebuildJob).where(
        RebuildJob.cluster_id == host,
        RebuildJob.status.in_(IN_FLIGHT_STATUSES),
    )
    if projects is not None:
        query = query.where(RebuildJob.project.in_(sorted(set(projects))))
    rows = (await session.execute(query)).scalars().all()
    return sum(1 for row in rows if _live(row, now))


async def admit_rebuild(
    session,
    *,
    host: str,
    project: str,
    instance_name: str,
    tenant: bool,
    # The projects whose rebuilds count against a Tenant's cap; None counts the whole cluster.
    organization_projects: Optional[set[str]],
    per_hour: int,
    concurrent: int,
    now: Optional[datetime] = None,
) -> None:
    """Raise 403, 409 or 429 when this rebuild may not start now; return when it may."""
    now = now or datetime.now(timezone.utc)
    if tenant and (per_hour <= 0 or concurrent <= 0):
        raise _turned_off()

    running = await _running(session, host, project, instance_name, now)
    if running is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "rebuild_in_progress",
                "message": "A rebuild of this instance is already running. Wait for it to finish.",
                "job_id": running.job_id,
            },
        )
    if not tenant:
        return

    wait = await _budget_wait(session, host, project, instance_name, per_hour, now)
    if wait > 0:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "rebuild_budget",
                "message": (
                    f"This instance was rebuilt {per_hour} times in the last hour. "
                    f"You can rebuild it again in {plain_wait(wait)}."
                ),
                "retry_after_seconds": wait,
            },
            headers={"Retry-After": str(wait)},
        )

    if await _running_in(session, host, organization_projects, now) >= concurrent:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "rebuild_concurrency",
                "message": (
                    f"{concurrent} rebuilds are already running for your organization on this cluster. "
                    "Wait for one to finish, then try again."
                ),
                "retry_after_seconds": 60,
            },
            headers={"Retry-After": "60"},
        )
