"""Transactions that open and close the retention window of a staged move.

Entering retention, finishing a rollback, and finishing cleanup each change the job, its locks,
Orcastra's per-instance records, and the outbox in one commit guarded by the worker's lease and
fence token. A crash leaves either the whole transition or none of it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import and_, delete, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.migration_models import MigrationJob, MigrationPlan, MigrationResourceLock
from app.services.project_grants import revoke_project_grants

from .metadata_cutover import InstanceIdentity, move_instance_records
from .repository import RepositoryConflict, enqueue_event, utcnow
from .security import canonical_resource_key, sanitize_payload

RETAINED_PHASES = ("source_retained", "cleanup_due", "cleanup_blocked")


def source_identity(job: MigrationJob, *, uuid: Optional[str] = None) -> InstanceIdentity:
    return InstanceIdentity(
        cluster_id=job.source_cluster_id,
        project=job.source_project,
        name=str(job.source_resource_name or ""),
        cluster_fingerprint=job.source_cluster_fingerprint,
        uuid=uuid,
    )


def target_identity(job: MigrationJob, *, uuid: Optional[str] = None) -> InstanceIdentity:
    return InstanceIdentity(
        cluster_id=job.target_cluster_id,
        project=job.target_project,
        name=str(job.target_resource_name or job.source_resource_name or ""),
        cluster_fingerprint=job.target_cluster_fingerprint,
        uuid=uuid,
    )


async def _fenced_update(
    db: AsyncSession,
    job_id: str,
    *,
    worker_id: str,
    fence_token: int,
    values: dict[str, Any],
) -> bool:
    now = utcnow()
    values = {**values, "updated_at": now, "revision": MigrationJob.revision + 1}
    result = await db.execute(
        update(MigrationJob)
        .where(
            MigrationJob.job_id == job_id,
            MigrationJob.locked_by == worker_id,
            MigrationJob.fence_token == fence_token,
            MigrationJob.status == "running",
            MigrationJob.lease_expires_at > now,
        )
        .values(**values)
    )
    return bool(result.rowcount)


def owner_of(job: MigrationJob) -> set[int]:
    return {job.source_organization_id} if isinstance(job.source_organization_id, int) else set()


def naming_holders(events: list[dict[str, Any]], held_by: set[int]) -> list[dict[str, Any]]:
    """Every outbox event of a job names the organizations that hold what it moved: the job's owner,
    and for a removed project those whose policies held it. By the time the outbox publishes, a
    removed project is in no bound (services/webhooks/emitter.py, services/orcahub_reach.py)."""
    held = sorted(held_by)
    return [{**event, "extra": {**(event.get("extra") or {}), "held_organizations": held}} for event in events]


async def _events(
    db: AsyncSession,
    *,
    job_id: str,
    phase: str,
    message: str,
    actor: Mapping[str, str],
    outbox_events: list[dict[str, Any]],
    extra: Optional[dict[str, Any]] = None,
) -> None:
    await enqueue_event(
        db,
        job_id=job_id,
        event_type="migration.phase_changed",
        phase=phase,
        message=message,
        actor=actor,
        extra=extra,
    )
    for event in outbox_events:
        await enqueue_event(db, job_id=job_id, actor=actor, **event)


async def enter_source_retention(
    db: AsyncSession,
    job: MigrationJob,
    *,
    worker_id: str,
    event_actor: Mapping[str, str],
    retention_deadline: datetime,
    target_uuid: Optional[str],
    safe_result: dict[str, Any],
    outbox_events: list[dict[str, Any]],
) -> dict[str, int]:
    """Retain the stopped source, release every lock but its fence, and cut metadata over."""
    if not await _fenced_update(
        db,
        job.job_id,
        worker_id=worker_id,
        fence_token=job.fence_token,
        values={
            "phase": "source_retained",
            "status": "waiting",
            "retention_deadline": retention_deadline,
            "locked_by": None,
            "lease_expires_at": None,
            "error_code": None,
            "error_message": None,
            "safe_result": sanitize_payload(safe_result),
        },
    ):
        await db.rollback()
        raise RepositoryConflict("Migration lease ownership was lost.")
    target = target_identity(job, uuid=target_uuid)
    # The target is now an ordinary instance. Only the retained source stays fenced.
    await db.execute(
        delete(MigrationResourceLock).where(
            MigrationResourceLock.job_id == job.job_id,
            or_(
                MigrationResourceLock.lock_scope == "project",
                and_(
                    MigrationResourceLock.lock_scope == "instance",
                    MigrationResourceLock.cluster_id == target.cluster_id,
                    MigrationResourceLock.project == target.project,
                    MigrationResourceLock.resource_name == target.name,
                ),
            ),
        )
    )
    counts = await move_instance_records(
        db,
        old=source_identity(job),
        new=target,
        organization_id=job.source_organization_id,
    )
    await _events(
        db,
        job_id=job.job_id,
        phase="source_retained",
        message="Cutover completed. The stopped source is retained for rollback.",
        actor=event_actor,
        outbox_events=naming_holders(outbox_events, owner_of(job)),
        extra={"metadata_moved": counts},
    )
    await db.commit()
    return counts


async def refence_target_for_rollback(db: AsyncSession, job: MigrationJob) -> None:
    """Fence the target again before rollback stops and deletes it (caller commits)."""
    target = target_identity(job)
    key = canonical_resource_key("instance", target.cluster_id, target.project, target.name)
    owner = (
        await db.execute(
            select(MigrationResourceLock.job_id).where(MigrationResourceLock.lock_key == key)
        )
    ).scalar_one_or_none()
    if owner == job.job_id:
        return
    if owner is not None:
        # important: another job retains or works on this instance now; deleting it would
        # destroy that job's rollback point (security review, pass 2).
        raise RepositoryConflict("Another migration holds the target instance. Finish it first.")
    await db.execute(
        insert(MigrationResourceLock).values(
            job_id=job.job_id,
            lock_key=key,
            lock_scope="instance",
            cluster_id=target.cluster_id,
            project=target.project,
            resource_name=target.name,
        )
    )


async def finish_rollback(
    db: AsyncSession,
    job: MigrationJob,
    *,
    worker_id: str,
    event_actor: Mapping[str, str],
    source_uuid: Optional[str],
    phase: str,
    status: str,
    message: str,
    revert_metadata: bool,
    outbox_events: list[dict[str, Any]],
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
    revoke_rule_ids: Optional[list[int]] = None,
) -> dict[str, int]:
    """End a rollback or an abort: terminal job, every lock released, metadata restored."""
    now = utcnow()
    if not await _fenced_update(
        db,
        job.job_id,
        worker_id=worker_id,
        fence_token=job.fence_token,
        values={
            "phase": phase,
            "status": status,
            "active_source_key": None,
            "active_target_key": None,
            "finished_at": now,
            "locked_by": None,
            "lease_expires_at": None,
            "error_code": error_code,
            "error_message": error_message,
        },
    ):
        await db.rollback()
        raise RepositoryConflict("Migration lease ownership was lost.")
    await db.execute(
        delete(MigrationResourceLock).where(MigrationResourceLock.job_id == job.job_id)
    )
    counts = {"tags": 0, "schedules": 0, "tenant_rules": 0}
    if revert_metadata:
        counts = await move_instance_records(
            db,
            old=target_identity(job),
            new=source_identity(job, uuid=source_uuid),
            organization_id=job.source_organization_id,
        )
    held_by = owner_of(job)
    if revoke_rule_ids:
        # As on a project delete: no rule may outlive the project this job created and removed.
        # On a shared cluster another organization's later project of that name would inherit it.
        await revoke_project_grants(
            db,
            cluster_id=job.target_cluster_id,
            project_name=job.target_project,
            commit=False,
            only_rule_ids=revoke_rule_ids,
            held_by=held_by,
        )
    await _events(
        db,
        job_id=job.job_id,
        phase=phase,
        message=message,
        actor=event_actor,
        outbox_events=naming_holders(outbox_events, held_by),
        extra={"metadata_restored": counts} if revert_metadata else None,
    )
    await db.commit()
    return counts


async def schedule_due_cleanups(db: AsyncSession, *, automatic: bool) -> int:
    """Move retained moves past their deadline to automatic cleanup, or to cleanup_due."""
    now = utcnow()
    due = (
        await db.execute(
            select(MigrationJob, MigrationPlan.preflight_payload)
            .join(MigrationPlan, MigrationPlan.plan_id == MigrationJob.plan_id)
            .where(
                MigrationJob.phase == "source_retained",
                MigrationJob.status == "waiting",
                MigrationJob.retention_deadline.is_not(None),
                MigrationJob.retention_deadline <= now,
            )
        )
    ).all()
    for job, approved in due:
        values: dict[str, Any] = {
            "updated_at": now,
            "revision": MigrationJob.revision + 1,
        }
        # important: the operator approved a rollback contract. A move approved as
        # finalize-by-hand stays that way even if automatic cleanup was enabled later.
        contract = (approved or {}).get("rollback_contract") or {}
        if automatic and contract.get("automatic_cleanup") is True:
            values.update(
                phase="cleaning_up",
                status="pending",
                locked_by=None,
                lease_expires_at=None,
                safe_result=sanitize_payload(
                    {
                        **(job.safe_result or {}),
                        "cleanup_trigger": "retention_expired",
                        "recovery_kind": "cleanup",
                    }
                ),
            )
            message = "The retention window ended. The retained source will be deleted."
        else:
            values.update(phase="cleanup_due")
            message = "The retention window ended. Finalize to delete the retained source."
        result = await db.execute(
            update(MigrationJob)
            .where(
                MigrationJob.job_id == job.job_id,
                MigrationJob.phase == "source_retained",
                MigrationJob.status == "waiting",
            )
            .values(**values)
        )
        if result.rowcount:
            await enqueue_event(
                db,
                job_id=job.job_id,
                event_type="migration.retention_expired",
                phase=str(values.get("phase")),
                message=message,
                actor={
                    "issuer": job.actor_issuer,
                    "subject": job.actor_subject,
                    "email": job.actor_email,
                    "provider_uuid": job.actor_provider_uuid,
                    "role": job.requester_role,
                },
            )
    await db.commit()
    return len(due)


__all__ = [
    "RETAINED_PHASES",
    "enter_source_retention",
    "finish_rollback",
    "refence_target_for_rollback",
    "schedule_due_cleanups",
    "source_identity",
    "target_identity",
]
