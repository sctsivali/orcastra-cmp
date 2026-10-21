"""Undo a project relocation: abort before activation, or roll back during retention.

The job created the target project, so everything inside it is trusted only while the project
carries this job's marker and holds nothing the resource journal does not name. A rollback
proves that before its first delete and otherwise returns the job to retention untouched. An
abort deletes only never-started targets that carry the job marker. Then sources are restarted
where they were running, the cloned profiles and the project are deleted, and the project's
records are pointed back, in that order, each step safe to repeat.
"""

from __future__ import annotations

from typing import Any, Optional

from app.core.database import async_session_maker
from app.models.migration_models import MigrationJob, MigrationResource
from app.services.shared_cluster_audit import serialize_cluster_configuration

from . import executor, executor_project, preflight, repository
from .completion import project_instance_events
from .context import _assert_mutation_lease, _resource_update, _resources
from .contracts import MigrationLeaseLost, MigrationUnknown, _blocking
from .project_policy import (
    MIGRATION_JOB_MARKER,
    created_by_job,
    disposable_images,
    features_enabled,
    owned_rows,
)
from .project_retention_store import (
    fence_project_for_recovery,
    finish_project_rollback,
    instance_pairs,
    return_to_retention,
)
from .rollback import (
    _actor,
    _await,
    _delete,
    _manual_recovery,
    _read,
    _start_source_if_needed,
    _status,
    _stop,
)


def _names(resource: MigrationResource) -> tuple[str, str]:
    source_name = str((resource.source_ref or {}).get("name") or "")
    return source_name, str((resource.target_ref or {}).get("name") or source_name)


def _of_type(resources: list[MigrationResource], kind: str) -> list[MigrationResource]:
    return sorted(
        (resource for resource in resources if resource.resource_type == kind),
        key=lambda resource: resource.ordinal,
    )


async def foreign_content(
    job: MigrationJob,
    resources: list[MigrationResource],
    client: Any,
    *,
    side: str,
    known_images: frozenset[str] = frozenset(),
) -> Optional[str]:
    """What a job-owned project holds that the journal does not name, or None.

    Profiles and images count only when the project's features make them its own: otherwise
    LXD lists the default project's. ``known_images`` are base images proven disposable earlier,
    when the instances that named them still existed.
    """
    project = job.target_project if side == "target" else job.source_project
    record = await _blocking(executor.read_project, client, project=project, label=f"migration.{side}_project_record")
    inventory = await _blocking(
        preflight._known_project_resources, client, project, label=f"migration.{side}_inventory"
    )
    instance_names = {_names(resource)[1 if side == "target" else 0] for resource in _of_type(resources, "instance")}
    profile_names = {_names(resource)[0] for resource in _of_type(resources, "profile")} | {"default"}
    extra = sorted(
        str(row.get("name"))
        for row in inventory.get("instances") or []
        if isinstance(row, dict) and row.get("name") not in instance_names
    )
    if features_enabled(record, "profiles"):
        extra += sorted(
            f"profile {row.get('name')}"
            for row in inventory.get("profiles") or []
            if isinstance(row, dict) and row.get("name") not in profile_names
        )
    if features_enabled(record, "images"):
        images = [row for row in inventory.get("images") or [] if isinstance(row, dict)]
        disposable = disposable_images(images, inventory.get("instances") or []) | known_images
        kept = [row for row in images if str(row.get("fingerprint") or "") not in disposable]
        if kept:
            extra.append(f"{len(kept)} image(s) of its own")
    extra += sorted(
        f"{len(rows)} {kind.replace('_', ' ')}" for kind, rows in owned_rows(record, inventory).items()
    )
    if not extra:
        return None
    return (
        f"Project '{project}' holds what this migration did not create ({', '.join(extra[:6])}), "
        "so nothing was deleted."
    )


async def delete_job_project(
    job: MigrationJob,
    resources: list[MigrationResource],
    client: Any,
    *,
    side: str,
    images: frozenset[str] = frozenset(),
) -> None:
    """Delete the profiles the journal names (not default), then the now-empty project.

    ``images`` names base images proven disposable before the instances were deleted; cached
    images always go. LXD refuses to delete a project that still holds an image.
    """
    project = job.target_project if side == "target" else job.source_project
    record = await _blocking(executor.read_project, client, project=project, label="migration.project_before_delete")
    if record is None:
        return
    # important: without its own profiles, a profile call for this project reaches the default
    # project's profiles, which every project on the cluster may share (security review).
    if features_enabled(record, "profiles"):
        for resource in reversed(_of_type(resources, "profile")):
            name, _target = _names(resource)
            if name == "default":
                continue
            await _assert_mutation_lease()
            await _blocking(
                executor_project.delete_profile, client, project=project, name=name, label=f"migration.delete_profile[{name}]"
            )
    if features_enabled(record, "images"):
        # Cached images live in the project and would make LXD refuse the project delete.
        for image in await _blocking(executor_project.list_images, client, project=project, label="migration.project_images"):
            fingerprint = str(image.get("fingerprint") or "")
            if not fingerprint or not (image.get("cached") or fingerprint in images):
                continue
            await _assert_mutation_lease()
            operation_id = await _blocking(
                executor_project.submit_delete_image,
                client,
                project=project,
                fingerprint=fingerprint,
                label="migration.delete_cached_image",
            )
            await _await(client, operation_id, project, "migration.wait_delete_cached_image")
    await _assert_mutation_lease()
    await _blocking(executor.delete_project, client, project=project, label="migration.delete_project")
    if await _blocking(executor.read_project, client, project=project, label="migration.project_deleted") is not None:
        raise MigrationUnknown(f"Project '{project}' still exists after its delete.")


async def fence_or_reason(job: MigrationJob, *, side: str) -> Optional[str]:
    """Fence the project under the cluster lock ordinary writes take, or say why it cannot be.

    The lock makes the fence and every write admission one critical section: a write admitted
    before the fence has landed, and none is admitted after it.
    """
    cluster_id = job.target_cluster_id if side == "target" else job.source_cluster_id
    try:
        async with async_session_maker() as db:
            async with serialize_cluster_configuration(db, cluster_id):
                await fence_project_for_recovery(db, job, side=side)
                await db.commit()
    except repository.RepositoryConflict as exc:
        return str(exc)
    return None


async def _refuse_to_retention(job: MigrationJob, worker_id: str, code: str, message: str) -> None:
    try:
        async with async_session_maker() as db:
            await return_to_retention(
                db, job, worker_id=worker_id, event_actor=_actor(job), error_code=code, message=message
            )
    except repository.RepositoryConflict as exc:
        raise MigrationLeaseLost(str(exc)) from exc


async def _finish(
    job: MigrationJob,
    worker_id: str,
    resources: list[MigrationResource],
    *,
    rollback: bool,
    project_deleted: bool,
) -> None:
    for resource in resources:
        if resource.resource_type != "instance":
            await _resource_update(resource.resource_id, state="completed" if rollback else "failed")
    pairs = instance_pairs(job, resources)
    state = job.safe_result or {}
    try:
        async with async_session_maker() as db:
            await finish_project_rollback(
                db,
                job,
                worker_id=worker_id,
                event_actor=_actor(job),
                pairs=pairs,
                phase="rolled_back" if rollback else "failed",
                status="success" if rollback else "failure",
                message=(
                    "The target project was removed and the retained source project is "
                    "authoritative again."
                    if rollback
                    else "The relocation stopped before activation and its changes were undone."
                ),
                revert_metadata=rollback,
                target_project_deleted=project_deleted,
                outbox_events=(
                    project_instance_events(
                        job,
                        role="target",
                        created=False,
                        names=[target.name for _source, target in pairs],
                        occurred_at=repository.utcnow().isoformat(),
                    )
                    if rollback
                    else []
                ),
                error_code=None if rollback else str(state.get("abort_code") or "transfer_failed"),
                error_message=None if rollback else str(state.get("abort_reason") or "Transfer failed."),
            )
    except repository.RepositoryConflict as exc:
        raise MigrationLeaseLost(str(exc)) from exc


async def run_project_abort(
    job: MigrationJob, worker_id: str, source_client: Any, target_client: Any
) -> None:
    resources = await _resources(job.job_id)
    instances = _of_type(resources, "instance")
    # A start the daemon refused left that target stopped and unused; only one that may have run
    # makes the abort manual.
    if any(
        (resource.safe_result or {}).get("target_start_attempted")
        and not (
            (resource.safe_result or {}).get("target_start_refused")
            and not (resource.safe_result or {}).get("target_started")
        )
        for resource in instances
    ):
        await _manual_recovery(
            job,
            worker_id,
            code="abort_after_activation",
            message="A target start was attempted, so the job cannot discard it automatically.",
        )
        return
    project = await _blocking(
        executor.read_project, target_client, project=job.target_project, label="migration.abort_project"
    )
    ours = project is not None and created_by_job(project, job.job_id)
    if ours:
        for resource in instances:
            state = dict(resource.safe_result or {})
            _source, target_name = _names(resource)
            target = await _read(target_client, job.target_project, target_name, f"migration.abort_target[{target_name}]")
            if target is None:
                continue
            if (target.get("config") or {}).get(MIGRATION_JOB_MARKER) != job.job_id or _status(target) != "stopped":
                await _manual_recovery(
                    job,
                    worker_id,
                    code="abort_target_unproven",
                    message=f"Target '{target_name}' cannot be proven to be an unused copy from this job.",
                )
                return
            state = await _delete(
                job,
                resource,
                state,
                target_client,
                cluster_id=job.target_cluster_id,
                project=job.target_project,
                name=target_name,
                intent_key="abort_target_delete_intent",
                kind="abort_target_delete",
            )
    for resource in instances:
        state = dict(resource.safe_result or {})
        if state.get("source_identity_hash"):
            state = await _start_source_if_needed(job, resource, state, source_client)
        await _resource_update(resource.resource_id, state="failed", safe_result=state)
    if ours:
        problem = await foreign_content(job, resources, target_client, side="target")
        if problem is not None:
            await _manual_recovery(job, worker_id, code="abort_target_project_not_empty", message=problem)
            return
        await delete_job_project(job, resources, target_client, side="target")
    await _finish(job, worker_id, resources, rollback=False, project_deleted=ours)


async def run_project_rollback(
    job: MigrationJob, worker_id: str, source_client: Any, target_client: Any
) -> None:
    """Remove the relocated project and make the retained source project authoritative again."""
    reason = await fence_or_reason(job, side="target")
    if reason is not None:
        await _refuse_to_retention(job, worker_id, "rollback_target_in_use", f"Rollback refused. {reason}")
        return
    resources = await _resources(job.job_id)
    instances = _of_type(resources, "instance")
    resuming = any(
        (resource.safe_result or {}).get("rollback_delete_submission_intent") for resource in instances
    )
    for resource in instances:
        state = resource.safe_result or {}
        source_name, _target = _names(resource)
        source = await _read(source_client, job.source_project, source_name, f"migration.rollback_source[{source_name}]")
        if source is None or executor.instance_identity_fingerprint(source) != state.get("source_identity_hash"):
            await _manual_recovery(
                job,
                worker_id,
                code="rollback_source_unproven",
                message=f"Retained source '{source_name}' is missing or was replaced.",
            )
            return
    project = await _blocking(
        executor.read_project, target_client, project=job.target_project, label="migration.rollback_project"
    )
    ours = project is not None and created_by_job(project, job.job_id)
    if project is not None and not ours:
        await _manual_recovery(
            job,
            worker_id,
            code="rollback_target_unproven",
            message=f"Project '{job.target_project}' on the target is not the one this migration created.",
        )
        return
    if ours:
        problem = await foreign_content(job, resources, target_client, side="target")
        for resource in instances:
            _source, target_name = _names(resource)
            target = await _read(target_client, job.target_project, target_name, f"migration.rollback_target[{target_name}]")
            if target is not None and executor.instance_identity_fingerprint(target) != (
                resource.safe_result or {}
            ).get("target_identity_hash"):
                problem = problem or f"Target '{target_name}' was replaced, so nothing was deleted."
        if problem is not None:
            if resuming:
                await _manual_recovery(job, worker_id, code="rollback_target_changed", message=problem)
                return
            await _refuse_to_retention(job, worker_id, "rollback_target_changed", f"Rollback refused. {problem}")
            return
        for resource in instances:
            state = dict(resource.safe_result or {})
            _source, target_name = _names(resource)
            target = await _read(target_client, job.target_project, target_name, f"migration.rollback_target[{target_name}]")
            if target is None:
                continue
            # Proven again right before the stop and delete, not only in the first pass.
            if executor.instance_identity_fingerprint(target) != state.get("target_identity_hash"):
                await _manual_recovery(
                    job,
                    worker_id,
                    code="rollback_target_unproven",
                    message=f"Target '{target_name}' was replaced during the rollback, so it was left.",
                )
                return
            if _status(target) != "stopped":
                await _stop(
                    job,
                    resource,
                    state,
                    target_client,
                    cluster_id=job.target_cluster_id,
                    project=job.target_project,
                    name=target_name,
                )
            await _delete(
                job,
                resource,
                state,
                target_client,
                cluster_id=job.target_cluster_id,
                project=job.target_project,
                name=target_name,
                intent_key="rollback_delete_submission_intent",
                kind="rollback_target_delete",
            )
    for resource in _of_type(await _resources(job.job_id), "instance"):
        state = await _start_source_if_needed(job, resource, dict(resource.safe_result or {}), source_client)
        await _resource_update(resource.resource_id, state="completed", safe_result=state)
    if ours:
        await delete_job_project(job, resources, target_client, side="target")
    await _finish(job, worker_id, resources, rollback=True, project_deleted=True)


__all__ = [
    "delete_job_project",
    "fence_or_reason",
    "foreign_content",
    "run_project_abort",
    "run_project_rollback",
]
