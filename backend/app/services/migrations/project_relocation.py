"""Relocate a whole project: stage everything on the target, then activate it.

1. ``preparing_target``: create the target project (it carries the job marker), then clone every
   profile exactly as the plan approved.
2. ``quiescing``: stop the running sources. The original status is persisted before each stop.
3. ``final_sync``: transfer the instances one by one, stopped ones first, through the same
   contract an instance move uses.
4. ``target_validating`` and ``cutting_over``: validate every target, start the ones whose source
   was running in ``boot.autostart.priority`` order, then retain every source and cut the
   project's records over in one transaction.

Every step reads daemon state and the recorded operation before it writes, so the runner resumes
after a crash without repeating a mutation that already took effect. A failure before any target
starts aborts: the abort runner removes only what this job created.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

from app.core.database import async_session_maker
from app.models.migration_models import MigrationJob, MigrationResource
from app.models.migration_schemas import MigrationPlanCreateRequest

from . import executor, project_policy, repository
from .activation import Transferred, finish_transfer
from .completion import project_instance_events
from .context import (
    _fenced_job_update,
    _fresh_worker_preflight,
    _job_plan,
    _resource_update,
    _resources,
)
from .contracts import (
    MigrationAuthorityError,
    MigrationLeaseLost,
    MigrationUnknown,
    TransferFailed,
    _blocking,
)
from .operation_wait import transfer_deadline_seconds
from .preflight_source import active_instance_operations
from .preparation import _prepare_sources
from .project_preparation import (
    clone_target_profiles,
    ensure_target_project,
    settle_interrupted_stops,
    watch_recorded,
)
from .project_retention_store import enter_project_retention, instance_pairs
from .rollback import abort_before_activation
from .security import sanitize_payload
from .target_contract import TargetContract, mismatches
from .transfer import _admit_worker_run, _approved_contract_hash, _transfer_staged, build_live_contract


def _names(resource: MigrationResource) -> tuple[str, str]:
    source_name = str((resource.source_ref or {}).get("name") or "")
    return source_name, str((resource.target_ref or {}).get("name") or source_name)


def _of_type(resources: list[MigrationResource], kind: str) -> list[MigrationResource]:
    return sorted(
        (resource for resource in resources if resource.resource_type == kind),
        key=lambda resource: resource.ordinal,
    )


def _status(record: Optional[dict[str, Any]]) -> str:
    return str((record or {}).get("status") or "").lower()


def actor(job: MigrationJob) -> dict[str, str]:
    return repository.event_actor(
        {
            "requester_role": job.requester_role,
            "actor_issuer": job.actor_issuer,
            "actor_subject": job.actor_subject,
            "actor_email": job.actor_email,
            "actor_provider_uuid": job.actor_provider_uuid,
        }
    )


async def _enter_phase(job: MigrationJob, worker_id: str, phase: str, message: str) -> None:
    if job.phase == phase:
        return
    await _fenced_job_update(job, worker_id, phase=phase, status="running", event_message=message)
    job.phase = phase


def _root_pool(resource: MigrationResource) -> Optional[str]:
    contract = (resource.target_ref or {}).get("contract")
    value = contract.get("root_pool") if isinstance(contract, dict) else None
    return value if isinstance(value, str) and value else None


async def _contract(
    job: MigrationJob,
    resource: MigrationResource,
    source_client: Any,
    target_client: Any,
    request: MigrationPlanCreateRequest,
) -> TargetContract:
    source_name, _target = _names(resource)
    contract, _source = await _blocking(
        build_live_contract,
        source_client,
        target_client,
        job=job,
        request=request,
        root_pool_name=_root_pool(resource),
        source_name=source_name,
        source_state=resource.safe_result or {},
        label=f"migration.project_contract[{source_name}]",
    )
    if contract.contract_hash != _approved_contract_hash(resource):
        raise MigrationAuthorityError(
            f"The target definition for '{source_name}' changed after approval.",
            code="target_contract_drift",
        )
    return contract


async def _adopt_transfer(
    job: MigrationJob,
    resource: MigrationResource,
    state: dict[str, Any],
    source_client: Any,
    target_client: Any,
    request: MigrationPlanCreateRequest,
) -> tuple[dict[str, Any], TargetContract]:
    """Decide a transfer whose submission was recorded but whose outcome was not observed."""
    source_name, target_name = _names(resource)
    same_cluster = job.source_cluster_id == job.target_cluster_id
    deadline = transfer_deadline_seconds(state.get("transfer_bytes"))
    watched = await watch_recorded(
        resource, kind="transfer", client=source_client if same_cluster else target_client, deadline_seconds=deadline
    )
    if not same_cluster:
        source_side = await watch_recorded(
            resource, kind="source_transfer", client=source_client, deadline_seconds=deadline
        )
        watched = "failed" if "failed" in {watched, source_side} else watched
    target = await _blocking(
        executor.read_instance, target_client, project=job.target_project, name=target_name, label=f"migration.adopt_target[{target_name}]"
    )
    if target is None:
        busy = await _blocking(
            active_instance_operations,
            target_client,
            {(job.target_project, target_name)},
            label=f"migration.adopt_busy[{target_name}]",
        )
        if busy or watched == "settled":
            raise MigrationUnknown(f"The transfer of '{source_name}' has not settled yet.")
        raise TransferFailed(f"The transfer of '{source_name}' failed and left no target.")
    if (target.get("config") or {}).get(project_policy.MIGRATION_JOB_MARKER) != job.job_id:
        raise MigrationAuthorityError(
            f"Target '{target_name}' exists but was not created by this migration.", code="target_not_owned"
        )
    if watched == "failed":
        raise TransferFailed(f"The transfer of '{source_name}' failed.")
    if _status(target) != "stopped":
        raise MigrationUnknown(f"Target '{target_name}' is not stopped yet.")
    contract = await _contract(job, resource, source_client, target_client, request)
    snapshots = await _blocking(
        executor.snapshot_inventory_fingerprint,
        target_client,
        project=job.target_project,
        name=target_name,
        label=f"migration.adopt_snapshots[{target_name}]",
    )
    if mismatches(target, contract) or snapshots != state.get("source_snapshot_inventory_hash"):
        raise MigrationUnknown(f"Target '{target_name}' does not match its contract yet.")
    state.update(
        {
            "contract_hash": contract.contract_hash,
            "target_identity_hash": executor.instance_identity_fingerprint(target),
            "target_definition_hash": executor.instance_fingerprint(target),
            "target_snapshot_inventory_hash": snapshots,
            "transfer_reconciled": True,
        }
    )
    await _resource_update(resource.resource_id, state="ready", safe_result=state)
    return state, contract


def _start_order(transferred: Transferred) -> Transferred:
    """Higher ``boot.autostart.priority`` starts first, as LXD orders a host boot."""

    def priority(item: tuple[MigrationResource, dict[str, Any], Optional[TargetContract]]) -> int:
        contract = item[2]
        raw = (contract.expected_config if contract is not None else {}).get("boot.autostart.priority")
        try:
            return int(str(raw))
        except (TypeError, ValueError):
            return 0

    return sorted(transferred, key=lambda item: (-priority(item), item[0].ordinal))


def retention_callback(
    job: MigrationJob, worker_id: str, target_client: Any
) -> Any:
    async def retain(transferred: Transferred) -> None:
        resources = await _resources(job.job_id)
        for resource in resources:
            if resource.resource_type != "instance":
                await _resource_update(resource.resource_id, state="retained")
        target_uuids: dict[str, Optional[str]] = {}
        for resource, _state, _contract in transferred:
            _source, target_name = _names(resource)
            target = await _blocking(
                executor.read_instance, target_client, project=job.target_project, name=target_name, label=f"migration.target_uuid[{target_name}]"
            )
            uuid = ((target or {}).get("config") or {}).get("volatile.uuid")
            target_uuids[target_name] = uuid if isinstance(uuid, str) else None
        pairs = instance_pairs(job, resources, target_uuids=target_uuids)
        try:
            async with async_session_maker() as db:
                await enter_project_retention(
                    db,
                    job,
                    worker_id=worker_id,
                    event_actor=actor(job),
                    retention_deadline=repository.utcnow() + timedelta(hours=job.retention_hours),
                    pairs=pairs,
                    safe_result={**(job.safe_result or {}), "verified": True},
                    outbox_events=project_instance_events(
                        job,
                        role="target",
                        created=True,
                        names=[target.name for _source, target in pairs],
                        occurred_at=repository.utcnow().isoformat(),
                    ),
                )
        except repository.RepositoryConflict as exc:
            raise MigrationLeaseLost(str(exc)) from exc

    return retain


async def _transfer_all(
    job: MigrationJob,
    source_client: Any,
    target_client: Any,
    request: MigrationPlanCreateRequest,
    prepared: dict[str, dict[str, Any]],
) -> Transferred:
    transferred: Transferred = []
    for resource in _of_type(await _resources(job.job_id), "instance"):
        state = dict(prepared.get(resource.resource_id) or resource.safe_result or {})
        if state.get("target_identity_hash"):
            contract = await _contract(job, resource, source_client, target_client, request)
        elif state.get("transfer_submission_intent") or state.get("target_submission_intent"):
            state, contract = await _adopt_transfer(
                job, resource, state, source_client, target_client, request
            )
        else:
            state, contract = await _transfer_staged(
                job,
                resource,
                state,
                source_client,
                target_client,
                request=request,
                root_pool_name=_root_pool(resource),
            )
        prepared[resource.resource_id] = state
        transferred.append((resource, state, contract))
    return transferred


async def _resume_activation(
    job: MigrationJob,
    worker_id: str,
    source_client: Any,
    target_client: Any,
    request: MigrationPlanCreateRequest,
) -> None:
    transferred: Transferred = []
    for resource in _of_type(await _resources(job.job_id), "instance"):
        state = dict(resource.safe_result or {})
        if not state.get("target_identity_hash"):
            raise MigrationUnknown("A transferred target has no adopted identity yet.")
        contract = await _contract(job, resource, source_client, target_client, request)
        transferred.append((resource, state, contract))
    await finish_transfer(
        job,
        worker_id,
        source_client,
        target_client,
        _start_order(transferred),
        retain=retention_callback(job, worker_id, target_client),
    )


async def advance(
    job: MigrationJob, worker_id: str, source_client: Any, target_client: Any
) -> None:
    plan = await _job_plan(job)
    request = MigrationPlanCreateRequest.model_validate(plan.request_payload)
    instances = _of_type(await _resources(job.job_id), "instance")
    activated = any((resource.safe_result or {}).get("target_start_attempted") for resource in instances)
    transferred = bool(instances) and all(
        (resource.safe_result or {}).get("target_identity_hash") for resource in instances
    )
    if job.phase in {"target_validating", "cutting_over"} or (
        job.phase != "queued" and (activated or transferred)
    ):
        await _resume_activation(job, worker_id, source_client, target_client, request)
        return
    if job.phase == "queued":
        report = await _fresh_worker_preflight(job, source_client, target_client)
        if report["blockers"]:
            await _fenced_job_update(
                job,
                worker_id,
                phase="failed",
                status="failure",
                error_code="preflight_blocked",
                error_message="Fresh preflight blocked execution.",
                safe_result=sanitize_payload({**(job.safe_result or {}), "blockers": report["blockers"]}),
                terminal=True,
                event_message="Execution stopped because fresh preflight found blockers.",
            )
            return
        await _admit_worker_run(job, worker_id, source_client, report)
    try:
        await _enter_phase(job, worker_id, "preparing_target", "Creating the target project and its profiles.")
        resources = await _resources(job.job_id)
        project_resource = next(iter(_of_type(resources, "project")), None)
        if project_resource is None:
            raise MigrationUnknown("The project resource journal is missing.")
        await ensure_target_project(job, source_client, target_client, project_resource)
        await clone_target_profiles(job, source_client, target_client, request, resources)
        await _enter_phase(job, worker_id, "quiescing", "Stopping the source workloads for a cold transfer.")
        await settle_interrupted_stops(job, source_client)
        instances = _of_type(await _resources(job.job_id), "instance")
        # important: a pinned source already carries transfer evidence. Preparing it again
        # would rewrite its journal state, so only unpinned sources go through preparation.
        pending = [
            resource
            for resource in instances
            if not (resource.safe_result or {}).get("source_snapshot_inventory_hash")
        ]
        prepared = {
            resource.resource_id: dict(resource.safe_result or {})
            for resource in instances
            if resource not in pending
        }
        if pending:
            prepared.update(await _prepare_sources(job, worker_id, source_client, pending, False))
        await _enter_phase(job, worker_id, "final_sync", "Transferring the workloads to the target project.")
        transferred = await _transfer_all(job, source_client, target_client, request, prepared)
    except (TransferFailed, MigrationAuthorityError) as exc:
        await abort_before_activation(job, worker_id, source_client, target_client, reason=exc)
        return
    await finish_transfer(
        job,
        worker_id,
        source_client,
        target_client,
        _start_order(transferred),
        retain=retention_callback(job, worker_id, target_client),
    )


__all__ = ["actor", "advance", "retention_callback", "watch_recorded"]
