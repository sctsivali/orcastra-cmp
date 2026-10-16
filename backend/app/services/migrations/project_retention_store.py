"""Transactions that open and close the retention window of a project relocation.

The instance-move transactions in ``retention_store`` cover one source and one target. A project
relocation retains every source instance at once: entering retention fences each retained
source with an exact instance lock and releases the project locks, so cluster-wide writes on the
source cluster are not frozen for the whole window. Rollback and Finalize fence the project
they tear down again with a project lock for as long as they run.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.migration_models import MigrationJob, MigrationResource, MigrationResourceLock
from app.services.project_grants import revoke_project_grants

from .metadata_cutover import InstanceIdentity
from .project_metadata_cutover import ProjectIdentity, move_project_records
from .repository import RepositoryConflict
from .retention_store import _events, _fenced_update
from .security import canonical_resource_key, sanitize_payload

Pair = tuple[InstanceIdentity, InstanceIdentity]


def _names(resource: MigrationResource) -> tuple[str, str]:
    source_name = str((resource.source_ref or {}).get("name") or "")
    return source_name, str((resource.target_ref or {}).get("name") or source_name)


def instance_pairs(
    job: MigrationJob,
    resources: list[MigrationResource],
    *,
    target_uuids: Optional[Mapping[str, Optional[str]]] = None,
    source_uuids: Optional[Mapping[str, Optional[str]]] = None,
) -> list[Pair]:
    """Source and target identity of every instance resource, in journal order."""
    pairs: list[Pair] = []
    for resource in sorted(resources, key=lambda row: row.ordinal):
        if resource.resource_type != "instance":
            continue
        source_name, target_name = _names(resource)
        pairs.append(
            (
                InstanceIdentity(
                    cluster_id=job.source_cluster_id,
                    project=job.source_project,
                    name=source_name,
                    cluster_fingerprint=job.source_cluster_fingerprint,
                    uuid=(source_uuids or {}).get(source_name),
                ),
                InstanceIdentity(
                    cluster_id=job.target_cluster_id,
                    project=job.target_project,
                    name=target_name,
                    cluster_fingerprint=job.target_cluster_fingerprint,
                    uuid=(target_uuids or {}).get(target_name),
                ),
            )
        )
    return pairs


def _projects(job: MigrationJob) -> tuple[ProjectIdentity, ProjectIdentity]:
    return (
        ProjectIdentity(job.source_cluster_id, job.source_project),
        ProjectIdentity(job.target_cluster_id, job.target_project),
    )


async def _insert_lock(
    db: AsyncSession, job: MigrationJob, *, scope: str, cluster_id: str, project: str, name: Optional[str]
) -> None:
    key = canonical_resource_key(scope, cluster_id, project, name)
    owner = (
        await db.execute(select(MigrationResourceLock.job_id).where(MigrationResourceLock.lock_key == key))
    ).scalar_one_or_none()
    if owner == job.job_id:
        return
    if owner is not None:
        raise RepositoryConflict("Another migration holds a lock this relocation needs.")
    await db.execute(
        insert(MigrationResourceLock).values(
            job_id=job.job_id,
            lock_key=key,
            lock_scope=scope,
            cluster_id=cluster_id,
            project=project,
            resource_name=name,
        )
    )


async def enter_project_retention(
    db: AsyncSession,
    job: MigrationJob,
    *,
    worker_id: str,
    event_actor: Mapping[str, str],
    retention_deadline: datetime,
    pairs: list[Pair],
    safe_result: dict[str, Any],
    outbox_events: list[dict[str, Any]],
) -> dict[str, int]:
    """Retain every stopped source, fence each one, and cut the project's records over."""
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
    await db.execute(delete(MigrationResourceLock).where(MigrationResourceLock.job_id == job.job_id))
    for source, _target in pairs:
        await _insert_lock(
            db, job, scope="instance", cluster_id=source.cluster_id, project=source.project, name=source.name
        )
    old, new = _projects(job)
    counts = await move_project_records(
        db,
        old=old,
        new=new,
        organization_id=job.source_organization_id,
        instances=pairs,
    )
    await _events(
        db,
        job_id=job.job_id,
        phase="source_retained",
        message="Cutover completed. The stopped source project is retained for rollback.",
        actor=event_actor,
        outbox_events=outbox_events,
        extra={"metadata_moved": counts},
    )
    await db.commit()
    return counts


async def fence_project_for_recovery(db: AsyncSession, job: MigrationJob, *, side: str) -> None:
    """Fence the project a rollback (target) or Finalize (source) tears down. Caller commits.

    important: refused while any other active job holds a lock inside that project, because the
    teardown would then delete what that job retains or works on (security review).
    """
    cluster_id, project = (
        (job.target_cluster_id, job.target_project)
        if side == "target"
        else (job.source_cluster_id, job.source_project)
    )
    from .planning_checks import project_job_inside

    found = await project_job_inside(db, [(cluster_id, project)], except_job=job.job_id)
    other = found[0] if found else None
    if other is not None:
        raise RepositoryConflict(
            f"Another migration ({other[:8]}) holds a lock in project '{project}'. Finish it first."
        )
    await _insert_lock(db, job, scope="project", cluster_id=cluster_id, project=project, name=None)


async def finish_project_rollback(
    db: AsyncSession,
    job: MigrationJob,
    *,
    worker_id: str,
    event_actor: Mapping[str, str],
    pairs: list[Pair],
    phase: str,
    status: str,
    message: str,
    revert_metadata: bool,
    outbox_events: list[dict[str, Any]],
    target_project_deleted: bool,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
) -> dict[str, int]:
    """End a project rollback or abort: terminal job, locks released, records restored."""
    from .repository import utcnow

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
            "finished_at": utcnow(),
            "locked_by": None,
            "lease_expires_at": None,
            "error_code": error_code,
            "error_message": error_message,
        },
    ):
        await db.rollback()
        raise RepositoryConflict("Migration lease ownership was lost.")
    await db.execute(delete(MigrationResourceLock).where(MigrationResourceLock.job_id == job.job_id))
    counts: dict[str, int] = {}
    old, new = _projects(job)
    if revert_metadata:
        counts = await move_project_records(
            db,
            old=new,
            new=old,
            organization_id=job.source_organization_id,
            instances=[(target, source) for source, target in pairs],
            )
    if target_project_deleted:
        # As on a project delete: no rule may outlive the project it names.
        await revoke_project_grants(
            db, cluster_id=new.cluster_id, project_name=new.project, commit=False
        )
    await _events(
        db,
        job_id=job.job_id,
        phase=phase,
        message=message,
        actor=event_actor,
        outbox_events=outbox_events,
        extra={"metadata_restored": counts} if revert_metadata else None,
    )
    await db.commit()
    return counts


async def finish_project_cleanup(
    db: AsyncSession,
    job: MigrationJob,
    *,
    worker_id: str,
    event_actor: Mapping[str, str],
    message: str,
    outbox_events: list[dict[str, Any]],
) -> None:
    """End Finalize: the source project is gone, so are its locks and every rule naming it."""
    from .repository import utcnow

    if not await _fenced_update(
        db,
        job.job_id,
        worker_id=worker_id,
        fence_token=job.fence_token,
        values={
            "phase": "completed",
            "status": "success",
            "active_source_key": None,
            "active_target_key": None,
            "finished_at": utcnow(),
            "locked_by": None,
            "lease_expires_at": None,
            "error_code": None,
            "error_message": None,
        },
    ):
        await db.rollback()
        raise RepositoryConflict("Migration lease ownership was lost.")
    await db.execute(delete(MigrationResourceLock).where(MigrationResourceLock.job_id == job.job_id))
    await revoke_project_grants(
        db, cluster_id=job.source_cluster_id, project_name=job.source_project, commit=False
    )
    await _events(
        db,
        job_id=job.job_id,
        phase="completed",
        message=message,
        actor=event_actor,
        outbox_events=outbox_events,
    )
    await db.commit()


async def return_to_retention(
    db: AsyncSession,
    job: MigrationJob,
    *,
    worker_id: str,
    event_actor: Mapping[str, str],
    error_code: str,
    message: str,
) -> None:
    """Refuse a rollback or Finalize that found drift before touching anything.

    The job goes back to retention, and the project fence the action added is released, so the
    operator can investigate and choose again.
    """
    if not await _fenced_update(
        db,
        job.job_id,
        worker_id=worker_id,
        fence_token=job.fence_token,
        values={
            "phase": "cleanup_blocked",
            "status": "waiting",
            "locked_by": None,
            "lease_expires_at": None,
            "error_code": error_code,
            "error_message": message,
        },
    ):
        await db.rollback()
        raise RepositoryConflict("Migration lease ownership was lost.")
    await db.execute(
        delete(MigrationResourceLock).where(
            MigrationResourceLock.job_id == job.job_id,
            MigrationResourceLock.lock_scope == "project",
        )
    )
    await _events(
        db,
        job_id=job.job_id,
        phase="cleanup_blocked",
        message=message,
        actor=event_actor,
        outbox_events=[],
    )
    await db.commit()


__all__ = [
    "enter_project_retention",
    "fence_project_for_recovery",
    "finish_project_cleanup",
    "finish_project_rollback",
    "instance_pairs",
    "return_to_retention",
]
