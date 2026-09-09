"""Creating, editing and reading a schedule. The router stays thin so the rules stay readable.

The ownership rule is the load-bearing part, and it is the same for Admin as for Partner: a
schedule may only be created against a cluster the actor personally reaches. Admin here is
least-privilege plus break-glass rather than a god view, and a schedule is not a momentary act. One
manual snapshot on a partner's cluster is a single attributable thing that expires in seven days; a
schedule is an indefinite unattended write into someone else's hardware with no grant and no notice
to its owner. Momentary actions may cross clusters. Standing commitments may not.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.org_scope import _query_owned_clusters, get_user_org_ids, resolve_cluster_orgs
from app.models.snapshot_schedule_models import SnapshotSchedule
from app.models.snapshot_schedule_schemas import SnapshotScheduleInfo
from app.services.snapshots import recurrence

logger = logging.getLogger(__name__)

# Reserved so an operator can always still take one by hand on an instance a schedule runs against.
# Mirrors the reasoning behind MAX_SCHEDULE_KEEP_LAST rather than restating the number.
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


async def assert_actor_owns_cluster(db: Any, actor_email: Optional[str], host: str) -> None:
    """A schedule may only be aimed at a cluster the actor personally reaches.

    important: `_query_owned_clusters` rather than `get_user_allowed_clusters`, deliberately. The
    latter short-circuits to None for an Admin, which means "everything" and would let an Admin
    place an indefinite unattended write on any partner's hardware. It is also role-derived, and
    the role lives only in a JWT, so a rule written in terms of it could not be re-checked at fire
    time. This one is a plain database question and reads the same from a request and from the loop.
    """
    if not actor_email:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A schedule needs an identifiable owner, and this session has no email.",
        )
    owned = await _query_owned_clusters(actor_email, db)
    if host not in owned:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "cluster_is_not_yours_to_schedule",
                "message": (
                    f"A recurring snapshot on '{host}' can only be created by someone whose "
                    "organization holds that cluster. Taking a single snapshot there is a "
                    "momentary act and is unaffected."
                ),
                "host": host,
            },
        )


def upstream_conflict(host: str, project: str, instance_name: str, policy: Dict[str, Any]):
    """The refusal when the metered product upstream already schedules this instance.

    409, not 403, and the absence of an acknowledgement field is the signal. Every other refusal in
    this family carries one naming the way through, and the console is built to grow the affordance
    a refusal names. A 403 with no such field would read, to that console and to the next person
    writing against it, as a gate whose acknowledgement they failed to find. This is a conflict in
    the target's state, which no acknowledgement can resolve, so it says so.
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

    Refusing here beats a schedule that looks healthy and then fails silently at 03:00, which is
    when nobody is watching and the operator learns about it from a missing restore point.
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


async def resolve_owning_org(db: Any, user: Any, host: str) -> Optional[int]:
    """The organization this schedule belongs to for the rest of its life.

    `resolve_cluster_orgs`, the plural one: the singular takes the lowest id, which its own comment
    calls fine for a label and wrong for a permission, and a cluster on this deployment already
    carries more than one binding.
    """
    bindings = await resolve_cluster_orgs(host, db)
    if not bindings:
        return None
    mine = set(await get_user_org_ids(user, db))
    for org_id, _slug in bindings:
        if org_id in mine:
            return org_id
    # An Admin may hold the cluster through a registration rather than a membership. Falling back
    # to the first binding keeps the schedule attributable to the organization that owns the
    # hardware rather than leaving it ownerless, which fire-time authorization treats as terminal.
    return bindings[0][0]


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
        # The unique constraint, not a read-then-write. Four workers make the check-then-insert
        # race real, and the second schedule is what would blow the per-instance cap.
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
    """One schedule, refusing anything the actor's own clusters do not include.

    An unknown id and an unauthorised one answer the same 404 on purpose, so the endpoint cannot be
    used to learn which instances elsewhere on the fleet carry a schedule.
    """
    schedule = (
        await db.execute(select(SnapshotSchedule).where(SnapshotSchedule.id == schedule_id))
    ).scalar_one_or_none()
    if schedule is None:
        raise HTTPException(status_code=404, detail="No such snapshot schedule.")
    owned = await _query_owned_clusters(actor_email or "", db)
    if schedule.cluster_id not in owned:
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
    owned = await _query_owned_clusters(actor_email or "", db)
    if not owned:
        return []
    stmt = select(SnapshotSchedule).where(SnapshotSchedule.cluster_id.in_(owned))
    if host:
        stmt = stmt.where(SnapshotSchedule.cluster_id == host)
    if project:
        stmt = stmt.where(SnapshotSchedule.project == project)
    if instance:
        stmt = stmt.where(SnapshotSchedule.instance_name == instance)
    rows = (await db.execute(stmt.order_by(SnapshotSchedule.next_run_at))).scalars().all()
    return list(rows)


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
        # The old slot named a run of the old recurrence. Carrying it would name the next snapshot
        # after a moment this schedule no longer has.
        schedule.due_slot = None
    schedule.updated_at = now
