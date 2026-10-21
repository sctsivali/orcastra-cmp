"""Undo a staged migration: automatic abort before activation, operator rollback in retention.

Both runners read state before every step and are safe to run again after a crash: a target
already gone is not deleted twice, a source already running is not started twice. A delete only
ever reaches an object proven to be this job's target, by its pinned identity (rollback) or by
the job marker on a never-started target (abort). Anything that cannot be proven moves the job
to manual recovery with every fence still held.
"""

from __future__ import annotations

from typing import Any, Optional

from app.core.config import get_settings
from app.core.database import async_session_maker
from app.models.migration_models import MigrationJob, MigrationResource

from . import executor, repository
from .completion import removal_events
from .context import _assert_mutation_lease, _fenced_job_update, _resource_update, _resources
from .contracts import MigrationLeaseLost, MigrationUnknown, TransferFailed, _blocking
from .operation_wait import await_operation
from .project_policy import MIGRATION_JOB_MARKER, created_by_job
from .retention_store import finish_rollback
from .security import sanitize_error, sanitize_payload
from .source_fence import unfence_source_nics

__all__ = [
    "TransferFailed",
    "abort_before_activation",
    "remove_created_project_if_empty",
    "run_abort",
    "run_rollback",
]


def _names(resource: MigrationResource) -> tuple[str, str]:
    source_name = str((resource.source_ref or {}).get("name") or "")
    return source_name, str((resource.target_ref or {}).get("name") or source_name)


def _status(record: Optional[dict[str, Any]]) -> str:
    return str((record or {}).get("status") or "").lower()


def _actor(job: MigrationJob) -> dict[str, str]:
    return repository.event_actor(
        {
            "requester_role": job.requester_role,
            "actor_issuer": job.actor_issuer,
            "actor_subject": job.actor_subject,
            "actor_email": job.actor_email,
            "actor_provider_uuid": job.actor_provider_uuid,
        }
    )


async def _read(client: Any, project: str, name: str, label: str) -> Optional[dict[str, Any]]:
    return await _blocking(executor.read_instance, client, project=project, name=name, label=label)


async def _await(client: Any, operation_id: Optional[str], project: str, label: str) -> None:
    if not operation_id:
        return
    try:
        outcome = await await_operation(
            client, operation_id=operation_id, project=project, label=label, deadline_seconds=900
        )
    except executor.OperationForgotten:
        return
    if outcome.get("failed"):
        raise MigrationUnknown(outcome.get("err") or f"{label} failed.")


async def _manual_recovery(job: MigrationJob, worker_id: str, *, code: str, message: str) -> None:
    await _fenced_job_update(
        job,
        worker_id,
        phase="manual_recovery",
        status="manual_recovery",
        locked_by=None,
        lease_expires_at=None,
        error_code=code,
        error_message=message,
        event_message="Recovery stopped because the next step could not be proven safe.",
    )


async def _stop(
    job: MigrationJob,
    resource: MigrationResource,
    state: dict[str, Any],
    client: Any,
    *,
    cluster_id: str,
    project: str,
    name: str,
) -> None:
    """Graceful stop, then a forced stop if the guest ignores it."""
    timeout = int(get_settings().migration_default_stop_timeout_seconds)
    for force in (False, True):
        await _assert_mutation_lease()
        try:
            operation_id = await _blocking(
                executor.submit_instance_state,
                client,
                project=project,
                name=name,
                action="stop",
                action_timeout=timeout,
                force=force,
                label=f"migration.recovery_stop[{name}]",
            )
            if operation_id:
                await _resource_update(
                    resource.resource_id,
                    state="cleaning",
                    operation_ref={
                        "kind": "recovery_stop",
                        "cluster_id": cluster_id,
                        "project": project,
                        "operation_id": operation_id,
                    },
                )
            await _await(client, operation_id, project, f"migration.wait_recovery_stop[{name}]")
        except MigrationUnknown:
            if force:
                raise
        record = await _read(client, project, name, f"migration.recovery_stopped[{name}]")
        if record is None or _status(record) == "stopped":
            return
    raise MigrationUnknown(f"'{name}' did not stop for recovery.")


async def _delete(
    job: MigrationJob,
    resource: MigrationResource,
    state: dict[str, Any],
    client: Any,
    *,
    cluster_id: str,
    project: str,
    name: str,
    intent_key: str,
    kind: str,
) -> dict[str, Any]:
    state = {
        **state,
        intent_key: True,
        intent_key.replace("_intent", "_at"): repository.utcnow().isoformat(),
    }
    await _resource_update(resource.resource_id, state="cleaning", safe_result=state)
    await _assert_mutation_lease()
    try:
        operation_id = await _blocking(
            executor.submit_delete_instance,
            client,
            project=project,
            name=name,
            label=f"migration.{kind}[{name}]",
        )
    except Exception as exc:  # noqa: BLE001
        raise MigrationUnknown(f"Deleting '{name}' has an unknown outcome.") from exc
    if operation_id:
        await _resource_update(
            resource.resource_id,
            state="cleaning",
            operation_ref={
                "kind": kind,
                "cluster_id": cluster_id,
                "project": project,
                "operation_id": operation_id,
            },
        )
    await _await(client, operation_id, project, f"migration.wait_{kind}[{name}]")
    if await _read(client, project, name, f"migration.verify_{kind}[{name}]") is not None:
        raise MigrationUnknown(f"'{name}' still exists after its delete settled.")
    return state


async def _start_source_if_needed(
    job: MigrationJob, resource: MigrationResource, state: dict[str, Any], source_client: Any
) -> dict[str, Any]:
    source_name, _target = _names(resource)
    source = await _read(
        source_client, job.source_project, source_name, f"migration.recovery_source[{source_name}]"
    )
    if source is None or executor.instance_identity_fingerprint(source) != state.get(
        "source_identity_hash"
    ):
        raise MigrationUnknown(f"Source '{source_name}' no longer matches its pinned identity.")
    if state.get("source_autostart_fenced"):
        original = state.get("original_autostart_local")
        await _assert_mutation_lease()
        operation_id = await _blocking(
            executor.patch_instance_config,
            source_client,
            project=job.source_project,
            name=source_name,
            config={"boot.autostart": original if isinstance(original, str) else ""},
            label=f"migration.restore_autostart[{source_name}]",
        )
        await _await(source_client, operation_id, job.source_project, "migration.restore_autostart")
        state = {**state, "source_autostart_fenced": False}
        await _resource_update(resource.resource_id, state="cleaning", safe_result=state)
    state = await unfence_source_nics(job, resource, state, source_client)
    if state.get("original_status") == "running" and _status(source) != "running":
        state = {**state, "recovery_source_start_intent": True}
        await _resource_update(resource.resource_id, state="cleaning", safe_result=state)
        await _assert_mutation_lease()
        operation_id = await _blocking(
            executor.submit_instance_state,
            source_client,
            project=job.source_project,
            name=source_name,
            action="start",
            label=f"migration.recovery_start_source[{source_name}]",
        )
        await _await(source_client, operation_id, job.source_project, "migration.recovery_start")
        started = await _read(
            source_client, job.source_project, source_name, f"migration.recovery_started[{source_name}]"
        )
        if _status(started) != "running":
            raise MigrationUnknown(f"Source '{source_name}' did not start again.")
    return state


async def remove_created_project_if_empty(
    job: MigrationJob, resources: list[MigrationResource], target_client: Any
) -> None:
    """Delete a target project this job created, only when nothing else lives in it."""
    project_resource = next(
        (
            resource
            for resource in resources
            if resource.resource_type == "project"
            and (resource.safe_result or {}).get("target_project_created")
        ),
        None,
    )
    if project_resource is None:
        return
    record = await _blocking(
        executor.read_project, target_client, project=job.target_project, label="migration.created_project"
    )
    if record is None or not created_by_job(record, job.job_id):
        return
    from . import preflight

    inventory = await _blocking(
        preflight._known_project_resources,
        target_client,
        job.target_project,
        label="migration.created_project_inventory",
    )
    if preflight._occupied_resources(inventory):
        return
    await _assert_mutation_lease()
    await _blocking(
        executor.delete_project, target_client, project=job.target_project, label="migration.delete_created_project"
    )


async def abort_before_activation(
    job: MigrationJob, worker_id: str, source_client: Any, target_client: Any, *, reason: Exception
) -> None:
    safe_result = sanitize_payload(
        {
            **(job.safe_result or {}),
            "recovery_kind": "abort",
            "abort_reason": sanitize_error(reason),
            "abort_code": getattr(reason, "code", "transfer_failed"),
        }
    )
    await _fenced_job_update(
        job,
        worker_id,
        phase="rolling_back",
        status="running",
        safe_result=safe_result,
        event_message="A step failed before activation. Undoing what this job changed.",
    )
    # The runner reads the reason from this object when it records the outcome; without this
    # it saw the pre-abort copy and reported the generic "Transfer failed." (found live).
    job.safe_result = safe_result
    job.phase = "rolling_back"
    if getattr(job, "scope", None) == "project":
        from .project_rollback import run_project_abort

        await run_project_abort(job, worker_id, source_client, target_client)
        return
    await run_abort(job, worker_id, source_client, target_client)


async def run_abort(
    job: MigrationJob, worker_id: str, source_client: Any, target_client: Any
) -> None:
    resources = await _resources(job.job_id)
    for resource in resources:
        if resource.resource_type != "instance":
            continue
        state = dict(resource.safe_result or {})
        refused = bool(state.get("target_start_refused")) and not state.get("target_started")
        if state.get("target_start_attempted") and not refused:
            await _manual_recovery(
                job,
                worker_id,
                code="abort_after_activation",
                message="A target start was attempted, so the job cannot discard it automatically.",
            )
            return
        _source_name, target_name = _names(resource)
        submitted = any(
            state.get(key)
            for key in ("transfer_submission_intent", "target_submission_intent")
        )
        if submitted and job.strategy != "member_relocation":
            target = await _read(
                target_client, job.target_project, target_name, f"migration.abort_target[{target_name}]"
            )
            if target is not None:
                ours = (target.get("config") or {}).get(MIGRATION_JOB_MARKER) == job.job_id
                if ours and refused and state.get("target_identity_hash"):
                    # A copy or import keeps user.* keys, marker included: discard only the very
                    # object this job adopted.
                    ours = executor.instance_identity_fingerprint(target) == state["target_identity_hash"]
                if not ours or _status(target) != "stopped":
                    await _manual_recovery(
                        job,
                        worker_id,
                        code="abort_target_unproven",
                        message=f"Target '{target_name}' cannot be proven to be an unused copy "
                        "from this job.",
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
        if state.get("source_identity_hash"):
            state = await _start_source_if_needed(job, resource, state, source_client)
        await _resource_update(resource.resource_id, state="failed", safe_result=state)
    await remove_created_project_if_empty(job, resources, target_client)
    try:
        async with async_session_maker() as db:
            await finish_rollback(
                db,
                job,
                worker_id=worker_id,
                event_actor=_actor(job),
                source_uuid=None,
                phase="failed",
                status="failure",
                message="The migration stopped before activation and its changes were undone.",
                revert_metadata=False,
                outbox_events=[],
                error_code=str((job.safe_result or {}).get("abort_code") or "transfer_failed"),
                error_message=str((job.safe_result or {}).get("abort_reason") or "Transfer failed."),
            )
    except repository.RepositoryConflict as exc:
        raise MigrationLeaseLost(str(exc)) from exc


async def run_rollback(
    job: MigrationJob, worker_id: str, source_client: Any, target_client: Any
) -> None:
    """Remove the target, restore the retained source, and point Orcastra records back."""
    if job.strategy == "member_relocation":
        raise RuntimeError(
            "Member relocation has no retained source. Relocate it back with a new plan."
        )
    resources = await _resources(job.job_id)
    source_uuid: Optional[str] = None
    for resource in resources:
        if resource.resource_type != "instance":
            continue
        state = dict(resource.safe_result or {})
        source_name, target_name = _names(resource)
        source = await _read(
            source_client, job.source_project, source_name, f"migration.rollback_source[{source_name}]"
        )
        if source is None or executor.instance_identity_fingerprint(source) != state.get(
            "source_identity_hash"
        ):
            await _manual_recovery(
                job,
                worker_id,
                code="rollback_source_unproven",
                message=f"Retained source '{source_name}' is missing or was replaced.",
            )
            return
        target = await _read(
            target_client, job.target_project, target_name, f"migration.rollback_target[{target_name}]"
        )
        if target is not None:
            if executor.instance_identity_fingerprint(target) != state.get("target_identity_hash"):
                await _manual_recovery(
                    job,
                    worker_id,
                    code="rollback_target_unproven",
                    message=f"Target '{target_name}' is not the instance this migration created.",
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
            state = await _delete(
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
        state = await _start_source_if_needed(job, resource, state, source_client)
        source_uuid = ((source.get("config") or {}).get("volatile.uuid")) or source_uuid
        await _resource_update(resource.resource_id, state="completed", safe_result=state)
    await remove_created_project_if_empty(job, resources, target_client)
    try:
        async with async_session_maker() as db:
            await finish_rollback(
                db,
                job,
                worker_id=worker_id,
                event_actor=_actor(job),
                source_uuid=source_uuid if isinstance(source_uuid, str) else None,
                phase="rolled_back",
                status="success",
                message="The target was removed and the retained source is authoritative again.",
                revert_metadata=True,
                outbox_events=removal_events(
                    job, role="target", occurred_at=repository.utcnow().isoformat()
                ),
            )
    except repository.RepositoryConflict as exc:
        raise MigrationLeaseLost(str(exc)) from exc
