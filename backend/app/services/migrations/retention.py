"""Delete the retained source of a finished move: Finalize, or the end of retention.

The source must still be exactly the object that was retained: same immutable identity, same
definition (after the autostart fence), same snapshots, and stopped. The target only has to be
present as the same instance and readable, because it is a live workload by now and may have
legitimately changed. Any drift parks the job in ``cleanup_blocked`` with nothing deleted, so an
operator can investigate, roll back, or release the job.
"""

from __future__ import annotations

from typing import Any, Optional

from app.models.migration_models import MigrationJob, MigrationResource

from . import executor, repository
from .completion import removal_events
from .context import _assert_mutation_lease, _fenced_job_update, _resource_update, _resources
from .contracts import MigrationUnknown, _blocking
from .operation_wait import await_operation


def _names(resource: MigrationResource) -> tuple[str, str]:
    source_name = str((resource.source_ref or {}).get("name") or "")
    return source_name, str((resource.target_ref or {}).get("name") or source_name)


async def _blocked(job: MigrationJob, worker_id: str, code: str, message: str) -> None:
    await _fenced_job_update(
        job,
        worker_id,
        phase="cleanup_blocked",
        status="waiting",
        locked_by=None,
        lease_expires_at=None,
        error_code=code,
        error_message=message,
        event_message="Source cleanup was blocked and nothing was deleted.",
    )


async def _source_problem(
    job: MigrationJob, state: dict[str, Any], source: dict[str, Any], source_client: Any, name: str
) -> Optional[tuple[str, str]]:
    if str(source.get("status") or "").lower() != "stopped":
        return "source_not_fenced", f"Retained source '{name}' is not stopped."
    if executor.instance_identity_fingerprint(source) != state.get("source_identity_hash"):
        return "source_identity_drift", f"Retained source '{name}' was replaced."
    if executor.instance_fingerprint(source) != state.get("source_definition_hash"):
        return "source_definition_drift", f"Retained source '{name}' changed during retention."
    snapshots = await _blocking(
        executor.snapshot_inventory_fingerprint,
        source_client,
        project=job.source_project,
        name=name,
        label=f"migration.cleanup_source_snapshots[{name}]",
    )
    if snapshots != state.get("source_snapshot_inventory_hash"):
        return "source_snapshot_drift", f"Snapshots of retained source '{name}' changed."
    return None


async def run_cleanup(
    job: MigrationJob, worker_id: str, source_client: Any, target_client: Any
) -> None:
    resources = await _resources(job.job_id)
    instances = [resource for resource in resources if resource.resource_type == "instance"]
    if job.scope != "instance" or job.intent != "move" or job.strategy == "member_relocation":
        await _blocked(
            job, worker_id, "cleanup_not_applicable", "This migration has no retained source."
        )
        return
    plan: list[tuple[MigrationResource, dict[str, Any], str]] = []
    for resource in instances:
        state = dict(resource.safe_result or {})
        source_name, target_name = _names(resource)
        target = await _blocking(
            executor.read_instance,
            target_client,
            project=job.target_project,
            name=target_name,
            label=f"migration.cleanup_target[{target_name}]",
        )
        if target is None or executor.instance_identity_fingerprint(target) != state.get(
            "target_identity_hash"
        ):
            await _blocked(
                job,
                worker_id,
                "target_missing_or_replaced",
                f"Target '{target_name}' is missing or was replaced, so the source was kept.",
            )
            return
        if str(target.get("status") or "").lower() not in {"running", "stopped"}:
            await _blocked(
                job,
                worker_id,
                "target_unhealthy",
                f"Target '{target_name}' is {target.get('status')}, so the source was kept.",
            )
            return
        source = await _blocking(
            executor.read_instance,
            source_client,
            project=job.source_project,
            name=source_name,
            label=f"migration.cleanup_source[{source_name}]",
        )
        if source is None:
            if state.get("cleanup_submission_intent"):
                continue
            await _blocked(
                job,
                worker_id,
                "source_missing_before_cleanup",
                f"Retained source '{source_name}' disappeared before cleanup.",
            )
            return
        problem = await _source_problem(job, state, source, source_client, source_name)
        if problem is not None:
            await _blocked(job, worker_id, *problem)
            return
        plan.append((resource, state, source_name))

    for resource, state, source_name in plan:
        state = {
            **state,
            "cleanup_submission_intent": True,
            "cleanup_submission_at": repository.utcnow().isoformat(),
        }
        await _resource_update(resource.resource_id, state="cleaning", safe_result=state)
        await _assert_mutation_lease()
        try:
            operation_id = await _blocking(
                executor.submit_delete_instance,
                source_client,
                project=job.source_project,
                name=source_name,
                label=f"migration.cleanup_source_delete[{source_name}]",
            )
        except Exception as exc:  # noqa: BLE001
            raise MigrationUnknown(f"Cleanup of '{source_name}' has an unknown outcome.") from exc
        if operation_id:
            await _resource_update(
                resource.resource_id,
                state="cleaning",
                operation_ref={
                    "kind": "cleanup_source",
                    "cluster_id": job.source_cluster_id,
                    "project": job.source_project,
                    "operation_id": operation_id,
                },
            )
            try:
                outcome = await await_operation(
                    source_client,
                    operation_id=operation_id,
                    project=job.source_project,
                    label=f"migration.wait_cleanup_source[{source_name}]",
                    deadline_seconds=1800,
                )
            except executor.OperationForgotten:
                outcome = {"settled": True, "failed": False}
            if outcome.get("failed"):
                raise MigrationUnknown(outcome.get("err") or f"Could not delete '{source_name}'.")
        remaining = await _blocking(
            executor.read_instance,
            source_client,
            project=job.source_project,
            name=source_name,
            label=f"migration.cleanup_verify[{source_name}]",
        )
        if remaining is not None:
            raise MigrationUnknown(f"Source '{source_name}' still exists after its delete settled.")
        await _resource_update(resource.resource_id, state="completed", safe_result=state)
    for resource in instances:
        if resource.state != "completed":
            await _resource_update(
                resource.resource_id, state="completed", safe_result=resource.safe_result or {}
            )
    trigger = str((job.safe_result or {}).get("cleanup_trigger") or "finalize")
    await _fenced_job_update(
        job,
        worker_id,
        phase="completed",
        status="success",
        terminal=True,
        outbox_events=removal_events(
            job, role="source", occurred_at=repository.utcnow().isoformat()
        ),
        event_message=(
            "Retention ended and the retained source was deleted."
            if trigger == "retention_expired"
            else "The retained source was deleted on Finalize."
        ),
    )


__all__ = ["run_cleanup"]
