"""After the transfer: validate every target, activate, then complete or retain the source.

Every target is validated before any one of them starts, so a later failure cannot leave a
started target beside an unverified one. A move starts the targets whose source was running;
a copy restarts the sources it stopped. Then the whole set is read once more before the job
completes (copy, member relocation) or enters retention (staged move).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any, Optional

from app.core.database import async_session_maker
from app.models.migration_models import MigrationJob, MigrationResource
from app.services.lxd.root_disk import root_pool

from . import executor, repository
from .completion import target_completion_events
from .context import _assert_mutation_lease, _fenced_job_update, _resource_update
from .contracts import MigrationLeaseLost, MigrationUnknown, _blocking
from .operation_wait import await_operation
from .project_policy import MIGRATION_JOB_MARKER
from .retention_store import enter_source_retention
from .security import sanitize_payload
from .source_fence import fence_source_nics, nic_fence_applies
from .target_contract import TargetContract, mismatches

Transferred = list[tuple[MigrationResource, dict[str, Any], Optional[TargetContract]]]


def _names(resource: MigrationResource) -> tuple[str, str]:
    source_name = str((resource.source_ref or {}).get("name") or "")
    return source_name, str((resource.target_ref or {}).get("name") or source_name)


def _status(record: Optional[dict[str, Any]]) -> str:
    return str((record or {}).get("status") or "").lower()


async def _read(client: Any, project: str, name: str, label: str) -> Optional[dict[str, Any]]:
    return await _blocking(
        executor.read_instance, client, project=project, name=name, label=label
    )


async def _validate_pair(
    job: MigrationJob,
    resource: MigrationResource,
    state: dict[str, Any],
    contract: Optional[TargetContract],
    source_client: Any,
    target_client: Any,
    *,
    target_status: str | set[str],
    source_status: Optional[str | set[str]],
    label: str,
) -> None:
    source_name, target_name = _names(resource)
    target_statuses = {target_status} if isinstance(target_status, str) else target_status
    source_statuses = {source_status} if isinstance(source_status, str) else source_status
    target = await _read(
        target_client, job.target_project, target_name, f"migration.{label}_target[{target_name}]"
    )
    if (
        target is None
        or executor.instance_identity_fingerprint(target) != state.get("target_identity_hash")
        or _status(target) not in target_statuses
    ):
        raise MigrationUnknown(f"Target '{target_name}' changed before {label.replace('_', ' ')}.")
    if job.strategy == "member_relocation":
        if str(target.get("location") or "") != str(job.target_member or "") or root_pool(
            target
        ) != state.get("source_root_pool"):
            raise MigrationUnknown(f"Target '{target_name}' left its approved member or pool.")
        return
    if contract is not None and mismatches(target, contract):
        raise MigrationUnknown(f"Target '{target_name}' drifted from its approved definition.")
    if (target.get("config") or {}).get(MIGRATION_JOB_MARKER) != job.job_id:
        raise MigrationUnknown(f"Target '{target_name}' is not the object this job created.")
    source = await _read(
        source_client, job.source_project, source_name, f"migration.{label}_source[{source_name}]"
    )
    if (
        source is None
        or executor.instance_identity_fingerprint(source) != state.get("source_identity_hash")
        or (source_statuses is not None and _status(source) not in source_statuses)
    ):
        raise MigrationUnknown(f"Source '{source_name}' crossed its fence before {label}.")


async def _change_state(
    job: MigrationJob,
    resource: MigrationResource,
    state: dict[str, Any],
    client: Any,
    *,
    cluster_id: str,
    project: str,
    name: str,
    action: str,
    intent_key: str,
    kind: str,
) -> dict[str, Any]:
    state = {**state, intent_key: True}
    await _resource_update(resource.resource_id, state="cutting_over", safe_result=state)
    await _assert_mutation_lease()
    try:
        operation_id = await _blocking(
            executor.submit_instance_state,
            client,
            project=project,
            name=name,
            action=action,
            label=f"migration.{kind}[{name}]",
        )
    except Exception as exc:  # noqa: BLE001
        raise MigrationUnknown(f"'{action}' of '{name}' has an unknown submission outcome.") from exc
    if operation_id:
        await _resource_update(
            resource.resource_id,
            state="cutting_over",
            operation_ref={
                "kind": kind,
                "cluster_id": cluster_id,
                "project": project,
                "operation_id": operation_id,
            },
        )
        try:
            outcome = await await_operation(
                client,
                operation_id=operation_id,
                project=project,
                label=f"migration.wait_{kind}[{name}]",
                deadline_seconds=600,
            )
        except executor.OperationForgotten:
            outcome = {"settled": True, "failed": False}
        if outcome.get("failed"):
            raise MigrationUnknown(outcome.get("err") or f"Could not {action} '{name}'.")
    record = await _read(client, project, name, f"migration.verify_{kind}[{name}]")
    if _status(record) != ("running" if action == "start" else "stopped"):
        raise MigrationUnknown(f"'{name}' did not reach the state requested by {action}.")
    return state


async def _activate(
    job: MigrationJob,
    transferred: Transferred,
    source_client: Any,
    target_client: Any,
) -> None:
    for index, (resource, state, contract) in enumerate(transferred):
        if state.get("original_status") != "running":
            continue
        source_name, target_name = _names(resource)
        if job.intent == "move" and state.get("target_started"):
            continue
        if job.intent == "copy" and state.get("source_restarted"):
            continue
        if job.intent == "move" and state.get("target_start_attempted"):
            current = await _read(
                target_client, job.target_project, target_name, f"migration.resume_start[{target_name}]"
            )
            if _status(current) == "running":
                state = {**state, "target_started": True}
                await _resource_update(resource.resource_id, state="cutting_over", safe_result=state)
                transferred[index] = (resource, state, contract)
                continue
        if job.intent == "copy" and state.get("source_restart_attempted"):
            current = await _read(
                source_client, job.source_project, source_name, f"migration.resume_restart[{source_name}]"
            )
            if _status(current) == "running":
                state = {**state, "source_restarted": True}
                await _resource_update(resource.resource_id, state="cutting_over", safe_result=state)
                transferred[index] = (resource, state, contract)
                continue
        if job.intent == "move":
            state = await _change_state(
                job,
                resource,
                state,
                target_client,
                cluster_id=job.target_cluster_id,
                project=job.target_project,
                name=target_name,
                action="start",
                intent_key="target_start_attempted",
                kind="start_target",
            )
            state = {**state, "target_started": True}
        else:
            state = await _change_state(
                job,
                resource,
                state,
                source_client,
                cluster_id=job.source_cluster_id,
                project=job.source_project,
                name=source_name,
                action="start",
                intent_key="source_restart_attempted",
                kind="restart_source",
            )
            state = {**state, "source_restarted": True}
        await _resource_update(resource.resource_id, state="cutting_over", safe_result=state)
        transferred[index] = (resource, state, contract)


async def _fence_source_autostart(
    job: MigrationJob, transferred: Transferred, source_client: Any
) -> None:
    """A retained source must not start when its host reboots. Rollback restores the value."""
    for index, (resource, state, contract) in enumerate(transferred):
        source_name, _target = _names(resource)
        source = await _read(
            source_client, job.source_project, source_name, f"migration.autostart[{source_name}]"
        )
        if source is None:
            raise MigrationUnknown(f"Retained source '{source_name}' disappeared.")
        expanded = source.get("expanded_config") or source.get("config") or {}
        if str(expanded.get("boot.autostart") or "").lower() != "true":
            continue
        local = (source.get("config") or {}).get("boot.autostart")
        state = {
            **state,
            "source_autostart_fence_intent": True,
            "original_autostart_local": local,
        }
        await _resource_update(resource.resource_id, state="cutting_over", safe_result=state)
        await _assert_mutation_lease()
        operation_id = await _blocking(
            executor.patch_instance_config,
            source_client,
            project=job.source_project,
            name=source_name,
            config={"boot.autostart": "false"},
            label=f"migration.fence_autostart[{source_name}]",
        )
        if operation_id:
            await await_operation(
                source_client,
                operation_id=operation_id,
                project=job.source_project,
                label=f"migration.wait_fence_autostart[{source_name}]",
                deadline_seconds=300,
            )
        fenced = await _read(
            source_client, job.source_project, source_name, f"migration.autostart_check[{source_name}]"
        )
        fenced_config = (fenced or {}).get("expanded_config") or (fenced or {}).get("config") or {}
        if fenced is None or str(fenced_config.get("boot.autostart") or "").lower() == "true":
            raise MigrationUnknown(f"Could not turn off boot.autostart on '{source_name}'.")
        state = {
            **state,
            "source_autostart_fenced": True,
            "source_definition_hash": executor.instance_fingerprint(fenced),
        }
        await _resource_update(resource.resource_id, state="cutting_over", safe_result=state)
        transferred[index] = (resource, state, contract)


async def finish_transfer(
    job: MigrationJob,
    worker_id: str,
    source_client: Any,
    target_client: Any,
    transferred: Transferred,
    *,
    retain: Optional[Callable[[Transferred], Awaitable[None]]] = None,
) -> None:
    """Validate, activate, and complete or retain. ``retain`` replaces the instance retention
    transaction, for a project relocation that retains every source at once."""
    for resource, state, contract in transferred:
        started = bool(state.get("target_start_attempted") or state.get("target_started"))
        restarted = bool(
            state.get("source_restart_attempted") or state.get("source_restarted")
        )
        await _validate_pair(
            job, resource, state, contract, source_client, target_client,
            target_status={"stopped", "running"} if started else "stopped",
            source_status={"stopped", "running"} if restarted else "stopped",
            label="validation_barrier",
        )
    if job.intent == "move":
        await _fenced_job_update(
            job,
            worker_id,
            phase="cutting_over",
            status="running",
            event_message="All targets passed validation. Activating the target workloads.",
        )
    if nic_fence_applies(job):
        for index, (resource, state, contract) in enumerate(transferred):
            state = await fence_source_nics(job, resource, state, source_client)
            transferred[index] = (resource, state, contract)
    await _activate(job, transferred, source_client, target_client)
    for resource, state, contract in transferred:
        running = state.get("original_status") == "running"
        await _validate_pair(
            job, resource, state, contract, source_client, target_client,
            target_status="running" if (job.intent == "move" and running) else "stopped",
            source_status=(
                ("running" if running else "stopped") if job.intent == "copy" else "stopped"
            )
            if job.strategy != "member_relocation"
            else None,
            label="final_barrier",
        )
    occurred_at = repository.utcnow().isoformat()
    if job.intent == "copy" or job.strategy == "member_relocation":
        for resource, state, _contract in transferred:
            await _resource_update(resource.resource_id, state="completed", safe_result=state)
        await _fenced_job_update(
            job,
            worker_id,
            phase="completed",
            status="success",
            safe_result=sanitize_payload({**(job.safe_result or {}), "verified": True}),
            terminal=True,
            outbox_events=target_completion_events(
                job, include_created_events=job.intent == "copy", occurred_at=occurred_at
            ),
            event_message="Migration completed and target readback was verified.",
        )
        return
    await _fence_source_autostart(job, transferred, source_client)
    for resource, state, _contract in transferred:
        await _resource_update(resource.resource_id, state="retained", safe_result=state)
    if retain is not None:
        await retain(transferred)
        return
    resource, _state, _contract = transferred[0]
    _source_name, target_name = _names(resource)
    target = await _read(
        target_client, job.target_project, target_name, f"migration.target_uuid[{target_name}]"
    )
    target_uuid = ((target or {}).get("config") or {}).get("volatile.uuid")
    try:
        async with async_session_maker() as db:
            await enter_source_retention(
                db,
                job,
                worker_id=worker_id,
                event_actor=repository.event_actor(
                    {
                        "requester_role": job.requester_role,
                        "actor_issuer": job.actor_issuer,
                        "actor_subject": job.actor_subject,
                        "actor_email": job.actor_email,
                        "actor_provider_uuid": job.actor_provider_uuid,
                    }
                ),
                retention_deadline=repository.utcnow() + timedelta(hours=job.retention_hours),
                target_uuid=target_uuid if isinstance(target_uuid, str) else None,
                safe_result={**(job.safe_result or {}), "verified": True},
                outbox_events=target_completion_events(
                    job, include_created_events=True, occurred_at=occurred_at
                ),
            )
    except repository.RepositoryConflict as exc:
        raise MigrationLeaseLost(str(exc)) from exc


__all__ = ["finish_transfer"]
