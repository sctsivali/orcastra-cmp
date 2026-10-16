"""Delete the retained source project of a finished relocation: Finalize, or end of retention.

Before the first delete the whole source project is proven unchanged: it holds exactly the
retained instances and cloned profiles (cached images aside), every retained source is the same
stopped object with the same snapshots, every profile still has its approved definition, and
every target is present and healthy. Any drift returns the job to retention with nothing
deleted. Then instances, profiles, cached images, and the project go, in that order, and every
access rule that named the project is removed with the job's final transaction.
"""

from __future__ import annotations

from typing import Any

from app.core.database import async_session_maker
from app.models.migration_models import MigrationJob, MigrationResource

from . import executor, executor_project, preflight, repository
from .completion import project_instance_events
from .context import _resource_update, _resources
from .contracts import MigrationLeaseLost, _blocking
from .profile_clone import profile_fingerprint
from .project_policy import disposable_images
from .project_retention_store import finish_project_cleanup, return_to_retention
from .project_rollback import _names, _of_type, delete_job_project, fence_or_reason, foreign_content
from .retention import _source_problem
from .rollback import _actor, _delete, _manual_recovery, _read, _status


async def _refuse(job: MigrationJob, worker_id: str, code: str, message: str) -> None:
    try:
        async with async_session_maker() as db:
            await return_to_retention(
                db,
                job,
                worker_id=worker_id,
                event_actor=_actor(job),
                error_code=code,
                message=f"Source cleanup refused. {message}",
            )
    except repository.RepositoryConflict as exc:
        raise MigrationLeaseLost(str(exc)) from exc


async def _drift(
    job: MigrationJob,
    resources: list[MigrationResource],
    source_client: Any,
    target_client: Any,
    *,
    known_images: frozenset[str] = frozenset(),
) -> tuple[str, str] | None:
    for resource in _of_type(resources, "instance"):
        state = resource.safe_result or {}
        source_name, target_name = _names(resource)
        target = await _read(target_client, job.target_project, target_name, f"migration.cleanup_target[{target_name}]")
        if target is None or executor.instance_identity_fingerprint(target) != state.get("target_identity_hash"):
            return "target_missing_or_replaced", f"Target '{target_name}' is missing or was replaced."
        if _status(target) not in {"running", "stopped"}:
            return "target_unhealthy", f"Target '{target_name}' is {target.get('status')}."
        source = await _read(source_client, job.source_project, source_name, f"migration.cleanup_source[{source_name}]")
        if source is None:
            if state.get("cleanup_submission_intent"):
                continue
            return "source_missing_before_cleanup", f"Retained source '{source_name}' disappeared."
        problem = await _source_problem(job, dict(state), source, source_client, source_name)
        if problem is not None:
            return problem
    for resource in _of_type(resources, "profile"):
        name, _target = _names(resource)
        current = await _blocking(
            executor_project.read_profile,
            source_client,
            project=job.source_project,
            name=name,
            label=f"migration.cleanup_profile[{name}]",
        )
        if current is None:
            continue
        if profile_fingerprint(current) != (resource.source_ref or {}).get("definition_fingerprint"):
            return "source_profile_drift", f"Profile '{name}' of the source project changed during retention."
    problem = await foreign_content(job, resources, source_client, side="source", known_images=known_images)
    if problem is not None:
        return "source_project_changed", problem
    return None


async def run_project_cleanup(
    job: MigrationJob, worker_id: str, source_client: Any, target_client: Any
) -> None:
    reason = await fence_or_reason(job, side="source")
    if reason is not None:
        await _refuse(job, worker_id, "source_project_in_use", reason)
        return
    resources = await _resources(job.job_id)
    project_resource = next(iter(_of_type(resources, "project")), None)
    known = frozenset((project_resource.safe_result or {}).get("disposable_images") or []) if project_resource else frozenset()
    drift = await _drift(job, resources, source_client, target_client, known_images=known)
    if drift is not None:
        await _refuse(job, worker_id, *drift)
        return
    # The base images are only provable while the instances that name them still exist, so the
    # set is recorded before the first delete and reused if Finalize resumes after a crash.
    inventory = await _blocking(
        preflight._known_project_resources, source_client, job.source_project, label="migration.cleanup_images"
    )
    base_images = known | frozenset(
        disposable_images(inventory.get("images") or [], inventory.get("instances") or [])
    )
    if project_resource is not None and base_images != known:
        await _resource_update(
            project_resource.resource_id,
            state="cleaning",
            safe_result={**(project_resource.safe_result or {}), "disposable_images": sorted(base_images)},
        )
    deleted_any = any(
        (resource.safe_result or {}).get("cleanup_submission_intent") for resource in _of_type(resources, "instance")
    )
    for resource in _of_type(resources, "instance"):
        state = dict(resource.safe_result or {})
        source_name, _target = _names(resource)
        current = await _read(source_client, job.source_project, source_name, f"migration.cleanup_exists[{source_name}]")
        if current is not None:
            # Proven again right before the delete, not only in the drift pass.
            if executor.instance_identity_fingerprint(current) != state.get("source_identity_hash"):
                message = f"Retained source '{source_name}' was replaced."
                if deleted_any:
                    # Something is already deleted: going back to retention would offer a rollback
                    # that cannot restore it.
                    await _manual_recovery(job, worker_id, code="source_identity_drift", message=message)
                else:
                    await _refuse(job, worker_id, "source_identity_drift", message)
                return
            state = await _delete(
                job,
                resource,
                state,
                source_client,
                cluster_id=job.source_cluster_id,
                project=job.source_project,
                name=source_name,
                intent_key="cleanup_submission_intent",
                kind="cleanup_source",
            )
            deleted_any = True
        await _resource_update(resource.resource_id, state="completed", safe_result=state)
    await delete_job_project(job, resources, source_client, side="source", images=base_images)
    for resource in resources:
        if resource.resource_type != "instance":
            await _resource_update(resource.resource_id, state="completed")
    trigger = str((job.safe_result or {}).get("cleanup_trigger") or "finalize")
    try:
        async with async_session_maker() as db:
            await finish_project_cleanup(
                db,
                job,
                worker_id=worker_id,
                event_actor=_actor(job),
                message=(
                    "Retention ended and the retained source project was deleted."
                    if trigger == "retention_expired"
                    else "The retained source project was deleted on Finalize."
                ),
                outbox_events=project_instance_events(
                    job,
                    role="source",
                    created=False,
                    names=[_names(resource)[0] for resource in _of_type(resources, "instance")],
                    occurred_at=repository.utcnow().isoformat(),
                ),
            )
    except repository.RepositoryConflict as exc:
        raise MigrationLeaseLost(str(exc)) from exc


__all__ = ["run_project_cleanup"]
