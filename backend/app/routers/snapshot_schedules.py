"""Operator snapshot schedules.

`require_partner()` sits on the router rather than on each handler, because "tenants never see
this" is the whole gate and a per-handler dependency is one a later route can forget to carry.

note: `_assert_snapshot_surface_enabled` and `_enforce_snapshot_access` from the per-instance
snapshot router are deliberately NOT called here. Both exist only to decide a tenant's ACL, and no
tenant reaches this router at all. Their absence would otherwise read as an oversight.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthenticatedUser
from app.core.database import get_db
from app.core.dependencies import check_read_only_mode, require_partner
from app.core.lxd_errors import lxd_http_error
from app.core.lxd_names import LXD_NAME_DESCRIPTION, LXD_NAME_PATTERN
from app.core.org_scope import (
    org_scope_dependency,
    project_is_visible,
    verify_cluster_access,
    verify_project_access,
)
from app.models.snapshot_schedule_schemas import (
    SnapshotScheduleActionResponse,
    SnapshotScheduleCreateRequest,
    SnapshotScheduleListResponse,
    SnapshotScheduleUpdateRequest,
)
from app.services.activity_logger import get_activity_logger
from app.services.lxd.availability import require_client
from app.services.lxd.lazy_client import client_for
from app.services.lxd.lxd_service import LXDService, get_lxd_service
from app.services.snapshots import schedule_authority, schedule_service as svc
from app.services.snapshots.schedule_runner import RunOutcome, _run_one_guarded
from app.services.migration_fence import (
    MigrationFenceConflict,
    guard_instance_mutation,
)

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

router = APIRouter(
    prefix="/snapshot-schedules",
    tags=["Snapshots - Schedules"],
    dependencies=[Depends(require_partner()), Depends(org_scope_dependency)],
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _audit(action: str, schedule: Any, result: str = "success", **details: Any) -> None:
    try:
        activity_logger.log_activity(
            action=action,
            target_type="instance",
            target_id=schedule.instance_name,
            target_host=schedule.cluster_id,
            target_project=schedule.project,
            result=result,
            details={"schedule_id": schedule.id, "schedule_label": schedule.label, **details},
        )
    except Exception:  # pragma: no cover
        logger.warning("Could not write the audit record for %s", action, exc_info=True)


@asynccontextmanager
async def _guard_schedule_mutation(
    *,
    cluster_id: str,
    project: str,
    instance_name: str,
) -> AsyncIterator[None]:
    """Keep the cluster admission lock across schedule service commits."""
    try:
        async with guard_instance_mutation(
            cluster_id=cluster_id,
            project=project,
            instance_name=instance_name,
        ):
            yield
    except MigrationFenceConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc


@router.get("", response_model=SnapshotScheduleListResponse)
async def list_schedules(
    host: Optional[str] = Query(None, pattern=LXD_NAME_PATTERN, description=LXD_NAME_DESCRIPTION),
    project: Optional[str] = Query(None, pattern=LXD_NAME_PATTERN),
    instance: Optional[str] = Query(None, pattern=LXD_NAME_PATTERN),
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Every schedule on a cluster the caller's organization holds. All three filters optional.

    The listing is narrowed by the caller's own clusters regardless, so this could have relied on
    the filter alone. It refuses a named cluster outside the org scope anyway, matching the fleet
    snapshot route beside it: an operator who names a cluster they cannot see should be told so,
    not handed an empty list that reads as "nothing is scheduled there".
    """
    if host is not None:
        verify_cluster_access(host)
        if project is not None:
            project = verify_project_access(host, project)
    rows = await svc.list_for_actor(
        db, user.email, host=host, project=project, instance=instance
    )
    rows = [row for row in rows if project_is_visible(row.cluster_id, row.project)]
    return SnapshotScheduleListResponse(
        total=len(rows), items=[svc.to_info(row) for row in rows]
    )


@router.post("", response_model=SnapshotScheduleActionResponse, status_code=201)
async def create_schedule(
    body: SnapshotScheduleCreateRequest,
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Create a schedule, refusing anything the cluster's own state contradicts.

    important: the cluster and normalized project gates run before any database or LXD read. A
    Partner on a shared cluster must not be able to use an instance name as a probe into another
    organization's project.
    """
    body.project = verify_project_access(body.host, body.project)
    organization_id = await svc.assert_actor_can_schedule_target(
        db,
        user=user,
        host=body.host,
        project=body.project,
    )
    async with _guard_schedule_mutation(
        cluster_id=body.host,
        project=body.project,
        instance_name=body.instance_name,
    ):
        client = await _client(lxd_service, body.host)
        from app.services.lxd import snapshots as snapshots_service
        from app.services.snapshots.schedule_runner import _blocking

        try:
            config, policy = await _blocking(
                snapshots_service.read_instance_config_and_policy,
                client,
                body.instance_name,
                body.project,
                label=f"read {body.instance_name}",
            )
            conflict = svc.upstream_conflict(body.host, body.project, body.instance_name, policy)
            if conflict is not None:
                raise conflict
            rows = await _blocking(
                snapshots_service.list_instance_snapshots,
                client,
                body.instance_name,
                body.project,
                label=f"list {body.instance_name}",
            )
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - mapped as the instance snapshot routes map it
            # A name the cluster does not have answered 500 (live, 2026-11-05).
            raise lxd_http_error(exc, "Instance", body.instance_name) from exc

        no_room = svc.capacity_conflict(body.instance_name, rows, body.keep_last)
        if no_room is not None:
            raise no_room

        from app.services.break_glass_service import cluster_fingerprint

        schedule = await svc.insert_schedule(
            db,
            body=body,
            owner_email=user.email,
            organization_id=organization_id,
            instance_uuid=config.get("volatile.uuid"),
            cluster_fingerprint=await cluster_fingerprint(body.host),
            now=_now(),
        )
    _audit("instance.snapshot.schedule.create", schedule, summary=svc.to_info(schedule).summary)
    return SnapshotScheduleActionResponse(
        success=True,
        message=f"Snapshots of {body.instance_name} scheduled {svc.to_info(schedule).summary}.",
        schedule=svc.to_info(schedule),
    )


async def _client(lxd_service: LXDService, host: str):
    return require_client(await client_for(lxd_service, host, label="snapshot-schedules"), host)


@router.patch("/{schedule_id}", response_model=SnapshotScheduleActionResponse)
async def update_schedule(
    body: SnapshotScheduleUpdateRequest,
    schedule_id: int = Path(..., ge=1),
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    schedule = await svc.get_owned(db, schedule_id, user.email)
    schedule.project = verify_project_access(schedule.cluster_id, schedule.project)
    async with _guard_schedule_mutation(
        cluster_id=schedule.cluster_id,
        project=schedule.project,
        instance_name=schedule.instance_name,
    ):
        # Re-read the policy: an instance can acquire an upstream schedule between the create and
        # edit, and an edit that ignored that would quietly re-arm a schedule the runner must refuse.
        client = await _client(lxd_service, schedule.cluster_id)
        from app.services.lxd import snapshots as snapshots_service
        from app.services.snapshots.schedule_runner import _blocking

        _config, policy = await _blocking(
            snapshots_service.read_instance_config_and_policy,
            client,
            schedule.instance_name,
            schedule.project,
            label=f"read {schedule.instance_name}",
        )
        conflict = svc.upstream_conflict(
            schedule.cluster_id, schedule.project, schedule.instance_name, policy
        )
        if conflict is not None:
            raise conflict

        svc.apply_update(schedule, body, _now())
        await db.commit()
        await db.refresh(schedule)
    _audit("instance.snapshot.schedule.update", schedule)
    return SnapshotScheduleActionResponse(
        success=True, message="Schedule updated.", schedule=svc.to_info(schedule)
    )


@router.post("/{schedule_id}/pause", response_model=SnapshotScheduleActionResponse)
async def pause_schedule(
    schedule_id: int = Path(..., ge=1),
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Idempotent. Pausing a paused schedule is not an error, it is the state being asked for."""
    schedule = await svc.get_owned(db, schedule_id, user.email)
    schedule.project = verify_project_access(schedule.cluster_id, schedule.project)
    async with _guard_schedule_mutation(
        cluster_id=schedule.cluster_id,
        project=schedule.project,
        instance_name=schedule.instance_name,
    ):
        schedule.state = "paused"
        schedule.paused_reason = f"Paused by {user.email}."
        schedule.due_slot = None
        schedule.updated_at = _now()
        await db.commit()
        await db.refresh(schedule)
    _audit("instance.snapshot.schedule.paused", schedule, by="operator")
    return SnapshotScheduleActionResponse(
        success=True, message="Schedule paused.", schedule=svc.to_info(schedule)
    )


@router.post("/{schedule_id}/resume", response_model=SnapshotScheduleActionResponse)
async def resume_schedule(
    schedule_id: int = Path(..., ge=1),
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Resuming re-asks the question that paused it, rather than trusting that it went away."""
    schedule = await svc.get_owned(db, schedule_id, user.email)
    schedule.project = verify_project_access(schedule.cluster_id, schedule.project)
    async with _guard_schedule_mutation(
        cluster_id=schedule.cluster_id,
        project=schedule.project,
        instance_name=schedule.instance_name,
    ):
        # The caller's current affiliation authorizes management of the row. The original owner's
        # persisted affiliation authorizes its unattended writes. Resume needs both: otherwise a
        # colleague could re-arm a schedule whose durable actor has left, only for the next tick to
        # pause it again.
        refusal = await schedule_authority.standing_authority_refusal(
            db,
            actor_email=schedule.owner_email,
            organization_id=schedule.organization_id,
            cluster_id=schedule.cluster_id,
            project=schedule.project,
        )
        if refusal is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": refusal.code, "message": refusal.message},
            )

        client = await _client(lxd_service, schedule.cluster_id)
        from app.services.lxd import snapshots as snapshots_service
        from app.services.snapshots import recurrence
        from app.services.snapshots.schedule_runner import _blocking

        _config, policy = await _blocking(
            snapshots_service.read_instance_config_and_policy,
            client,
            schedule.instance_name,
            schedule.project,
            label=f"read {schedule.instance_name}",
        )
        conflict = svc.upstream_conflict(
            schedule.cluster_id, schedule.project, schedule.instance_name, policy
        )
        if conflict is not None:
            raise conflict

        now = _now()
        schedule.state = "active"
        schedule.paused_reason = None
        schedule.consecutive_failures = 0
        schedule.last_error = None
        schedule.due_slot = None
        schedule.next_run_at = recurrence.next_slot_after(
            schedule.frequency, schedule.hour_utc, schedule.minute_utc, schedule.weekday, now
        )
        schedule.updated_at = now
        await db.commit()
        await db.refresh(schedule)
    _audit("instance.snapshot.schedule.update", schedule, resumed=True)
    return SnapshotScheduleActionResponse(
        success=True, message="Schedule resumed.", schedule=svc.to_info(schedule)
    )


@router.delete("/{schedule_id}", response_model=SnapshotScheduleActionResponse)
async def delete_schedule(
    schedule_id: int = Path(..., ge=1),
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Removes the plan. The snapshots it already took stay and expire on their own.

    note: there is deliberately no `keep_snapshots=false`. One click destroying restore points that
    each individually require their own decision is exactly what the per-snapshot ownership gate
    exists to prevent; an operator who wants them gone deletes them from the list, where each one
    is gated on its own.
    """
    schedule = await svc.get_owned(db, schedule_id, user.email)
    schedule.project = verify_project_access(schedule.cluster_id, schedule.project)
    async with _guard_schedule_mutation(
        cluster_id=schedule.cluster_id,
        project=schedule.project,
        instance_name=schedule.instance_name,
    ):
        _audit("instance.snapshot.schedule.delete", schedule)
        await db.delete(schedule)
        await db.commit()
    return SnapshotScheduleActionResponse(
        success=True,
        message="Schedule removed. The snapshots it took stay where they are.",
    )


@router.post("/{schedule_id}/run-now", response_model=SnapshotScheduleActionResponse)
async def run_schedule_now(
    schedule_id: int = Path(..., ge=1),
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Fire this schedule now, through the loop's own code rather than a copy of it.

    Useful before something risky, and it is also the only way to prove a schedule works without
    waiting six hours for its slot. `due_slot` is set to now, so the snapshot is named for this
    moment rather than for a slot that has not come round.
    """
    schedule = await svc.get_owned(db, schedule_id, user.email)
    schedule.project = verify_project_access(schedule.cluster_id, schedule.project)
    if schedule.state != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "schedule_is_paused",
                "message": "Resume this schedule before running it.",
            },
        )

    async with _guard_schedule_mutation(
        cluster_id=schedule.cluster_id,
        project=schedule.project,
        instance_name=schedule.instance_name,
    ):
        now = _now()
        schedule.due_slot = now
        await db.commit()
        outcome: RunOutcome = await _run_one_guarded(
            db,
            schedule,
            now=now,
            lxd_service=lxd_service,
        )

        from app.services.snapshots.schedule_scheduler import settle_row

        settle_row(schedule, outcome, now)
        await db.commit()
        await db.refresh(schedule)

    if outcome.result in ("success", "replayed"):
        return SnapshotScheduleActionResponse(
            success=True,
            message=f"Snapshot {outcome.snapshot} taken.",
            schedule=svc.to_info(schedule),
        )
    detail = outcome.refusal.message if outcome.refusal else (outcome.error or "The run failed.")
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "error": outcome.refusal.code if outcome.refusal else "schedule_run_failed",
            "message": detail,
        },
    )
