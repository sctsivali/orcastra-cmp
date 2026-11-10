"""Creating, editing and reading a schedule, kept out of the router so the rules stay readable.

A schedule may only be created against a cluster and project that an active organization of the
actor still holds, for Admin as for Partner. A manual snapshot on another party's cluster is one
attributable act that expires in seven days; a schedule is an indefinite unattended write with no
grant and no notice to the cluster's owner. Momentary actions may cross clusters, standing
commitments may not.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.snapshot_schedule_models import SnapshotSchedule
from app.models.snapshot_schedule_schemas import SnapshotScheduleInfo
from app.services.snapshots import recurrence, schedule_authority

logger = logging.getLogger(__name__)

# The per-instance cap. MAX_SCHEDULE_KEEP_LAST stays below it so an operator can always take a
# snapshot by hand on an instance a schedule runs against.
from app.services.lxd.snapshots import MAX_OPS_SNAPSHOTS_PER_INSTANCE, is_operator_snapshot


def _iso(moment: Optional[datetime]) -> Optional[str]:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_info(schedule: SnapshotSchedule) -> SnapshotScheduleInfo:
    return SnapshotScheduleInfo(
        id=schedule.id,
        host=schedule.cluster_id,
        project=schedule.project,
        instance_name=schedule.instance_name,
        label=schedule.label,
        frequency=schedule.frequency,
        hour_utc=schedule.hour_utc,
        minute_utc=schedule.minute_utc,
        weekday=schedule.weekday,
        summary=recurrence.describe(
            schedule.frequency, schedule.hour_utc, schedule.minute_utc, schedule.weekday
        ),
        keep_last=schedule.keep_last,
        expiry_days=schedule.expiry_days,
        state=schedule.state,
        paused_reason=schedule.paused_reason,
        next_run_at=_iso(schedule.next_run_at) if schedule.state == "active" else None,
        last_run_at=_iso(schedule.last_run_at),
        last_snapshot_name=schedule.last_snapshot_name,
        last_error=schedule.last_error,
        consecutive_failures=schedule.consecutive_failures or 0,
        missed_runs=schedule.missed_runs or 0,
        created_by=schedule.owner_email,
        created_at=_iso(schedule.created_at),
        updated_at=_iso(schedule.updated_at),
    )


async def assert_actor_can_schedule_target(
    db: Any,
    *,
    user: Any,
    host: str,
    project: str,
) -> int:
    """Authorize the durable organization/project target before any LXD read.

    Request-scoped ContextVars stop a Partner crossing a shared-cluster project boundary at request
    time; this database-only proof is the one the background worker repeats later. Admin follows
    the same rule, because the scheduler never inherits an Admin session's global visibility.
    """
    organization_id = await schedule_authority.authorizing_organization_id(
        db,
        actor_email=user.email,
        cluster_id=host,
        project=project,
    )
    if organization_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "schedule_target_is_not_owned",
                "message": (
                    f"No active organization for this operator owns project '{project}' "
                    f"on cluster '{host}'."
                ),
            },
        )
    return organization_id


def upstream_conflict(host: str, project: str, instance_name: str, policy: Dict[str, Any]):
    """The refusal when the metered product upstream already schedules this instance.

    A 409 with no acknowledgement field. Every other refusal in this family names an acknowledgement
    the console can offer, and a 403 without one would read as a gate with a missing
    acknowledgement. This is a conflict in the target's state that no acknowledgement resolves.
    """
    if not policy.get("externally_managed"):
        return None
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "error": "upstream_schedule_owns_this_instance",
            "message": (
                f"Snapshots on {instance_name} are already scheduled by the metered product "
                f"outside this console ({policy.get('schedule') or 'on a schedule'}"
                + (f", kept for {policy['expiry']}" if policy.get("expiry") else "")
                + "). Orcastra never writes that schedule and will not run a second one beside it."
            ),
            "instance": instance_name,
            "host": host,
            "project": project,
            "schedule": policy.get("schedule"),
            "expiry": policy.get("expiry"),
            "owner": "upstream",
            "remediation": (
                "Change or remove the schedule where it was created. Nothing here can do it."
            ),
        },
    )


def capacity_conflict(instance_name: str, rows: List[Dict[str, Any]], keep_last: int):
    """Refuse a schedule that could not fit beside the snapshots already on the instance.

    Refused at creation so the schedule does not look healthy and then fail unattended at its first
    run.
    """
    held = [str(r.get("name")) for r in rows if is_operator_snapshot(str(r.get("name") or ""))]
    room = MAX_OPS_SNAPSHOTS_PER_INSTANCE - len(held)
    if room >= keep_last:
        return None
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "error": "not_enough_room_for_this_schedule",
            "message": (
                f"{instance_name} already holds {len(held)} operator snapshots out of "
                f"{MAX_OPS_SNAPSHOTS_PER_INSTANCE}, so a schedule keeping {keep_last} would not "
                "fit. Delete some, or keep fewer."
            ),
            "instance": instance_name,
            "operator_snapshots": held,
            "keep_last": keep_last,
            "limit": MAX_OPS_SNAPSHOTS_PER_INSTANCE,
        },
    )


async def insert_schedule(
    db: Any,
    *,
    body: Any,
    owner_email: str,
    organization_id: Optional[int],
    instance_uuid: Optional[str],
    cluster_fingerprint: Optional[str],
    now: datetime,
) -> SnapshotSchedule:
    schedule = SnapshotSchedule(
        cluster_id=body.host,
        project=body.project,
        instance_name=body.instance_name,
        instance_uuid=instance_uuid,
        cluster_fingerprint=cluster_fingerprint,
        label=body.label,
        frequency=body.frequency,
        hour_utc=body.hour_utc,
        minute_utc=body.minute_utc,
        weekday=body.weekday,
        keep_last=body.keep_last,
        expiry_days=body.expiry_days,
        owner_email=owner_email,
        organization_id=organization_id,
        state="active",
        next_run_at=recurrence.next_slot_after(
            body.frequency, body.hour_utc, body.minute_utc, body.weekday, now
        ),
    )
    db.add(schedule)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        # The unique constraint decides: with four workers a check-then-insert can race, and a
        # second schedule would break the per-instance cap.
        existing = await find_for_instance(db, body.host, body.project, body.instance_name)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "snapshot_schedule_already_exists",
                "message": (
                    f"{body.instance_name} already has an operator schedule"
                    + (f", '{existing.label}', created by {existing.owner_email}" if existing else "")
                    + ". Edit that one rather than adding a second."
                ),
                "instance": body.instance_name,
                "schedule_id": existing.id if existing else None,
            },
        )
    await db.refresh(schedule)
    return schedule


async def find_for_instance(
    db: Any, host: str, project: str, instance_name: str
) -> Optional[SnapshotSchedule]:
    return (
        await db.execute(
            select(SnapshotSchedule).where(
                SnapshotSchedule.cluster_id == host,
                SnapshotSchedule.project == project,
                SnapshotSchedule.instance_name == instance_name,
            )
        )
    ).scalar_one_or_none()


async def get_owned(db: Any, schedule_id: int, actor_email: Optional[str]) -> SnapshotSchedule:
    """One schedule the current caller may manage.

    An unknown id and an unauthorised one both answer 404, so the endpoint cannot reveal which
    instances elsewhere on the fleet carry a schedule. Authority is checked for the current caller,
    so an authorized colleague can pause, edit or remove a schedule after its creator leaves; the
    background runner rechecks the original owner before writing.
    """
    schedule = (
        await db.execute(select(SnapshotSchedule).where(SnapshotSchedule.id == schedule_id))
    ).scalar_one_or_none()
    if schedule is None:
        raise HTTPException(status_code=404, detail="No such snapshot schedule.")
    refusal = await schedule_authority.standing_authority_refusal(
        db,
        actor_email=actor_email,
        organization_id=schedule.organization_id,
        cluster_id=schedule.cluster_id,
        project=schedule.project,
    )
    if refusal is not None:
        raise HTTPException(status_code=404, detail="No such snapshot schedule.")
    return schedule


async def list_for_actor(
    db: Any,
    actor_email: Optional[str],
    *,
    host: Optional[str] = None,
    project: Optional[str] = None,
    instance: Optional[str] = None,
) -> List[SnapshotSchedule]:
    """Schedules the current caller may manage, judged by the caller's authority."""
    stmt = select(SnapshotSchedule)
    if host:
        stmt = stmt.where(SnapshotSchedule.cluster_id == host)
    if project:
        stmt = stmt.where(SnapshotSchedule.project == project)
    if instance:
        stmt = stmt.where(SnapshotSchedule.instance_name == instance)
    rows = (await db.execute(stmt.order_by(SnapshotSchedule.next_run_at))).scalars().all()
    visible: List[SnapshotSchedule] = []
    for row in rows:
        refusal = await schedule_authority.standing_authority_refusal(
            db,
            actor_email=actor_email,
            organization_id=row.organization_id,
            cluster_id=row.cluster_id,
            project=row.project,
        )
        if refusal is None:
            visible.append(row)
    return visible


def apply_update(schedule: SnapshotSchedule, body: Any, now: datetime) -> None:
    """Edit in place, recomputing the next run only when the recurrence itself moved."""
    if body.label is not None:
        schedule.label = body.label
    if body.keep_last is not None:
        schedule.keep_last = body.keep_last
    if body.expiry_days is not None:
        schedule.expiry_days = body.expiry_days
    if body.frequency is not None:
        schedule.frequency = body.frequency
        schedule.hour_utc = body.hour_utc if body.hour_utc is not None else schedule.hour_utc
        schedule.minute_utc = body.minute_utc if body.minute_utc is not None else schedule.minute_utc
        schedule.weekday = body.weekday
        schedule.next_run_at = recurrence.next_slot_after(
            schedule.frequency, schedule.hour_utc, schedule.minute_utc, schedule.weekday, now
        )
        # A held slot belongs to the previous recurrence; keeping it would name the next snapshot
        # after a slot the new recurrence does not have.
        schedule.due_slot = None
    schedule.updated_at = now
