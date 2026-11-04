"""Validated workload transfer: fence the source, submit the copy or pull, verify the target."""

from __future__ import annotations

import logging
from typing import Any, Optional

from app.models.migration_models import MigrationJob, MigrationResource
from app.models.migration_schemas import MigrationPlanCreateRequest
from app.services.lxd.root_disk import root_pool

from . import executor, preflight, repository
from .activation import finish_transfer
from .billing import (
    billing_override_event,
    enforce_migration_billing_policy,
    report_has_running_source,
    report_needs_unmeasured_size_consent,
    validate_acknowledgements,
)
from .context import (
    _assert_mutation_lease,
    _fenced_job_update,
    _fresh_worker_preflight,
    _job_plan,
    _resource_update,
    _resources,
)
from .contracts import (
    MigrationAuthorityError,
    MigrationConflict,
    MigrationPreMutationRefusal,
    MigrationUnknown,
    _blocking,
)
from .operation_wait import await_operation, transfer_deadline_seconds
from .plan_contracts import _target_pool, approved_root_pool
from .preparation import _ensure_project_target, _prepare_sources
from .project_policy import MIGRATION_JOB_MARKER
from .rollback import TransferFailed, abort_before_activation
from .security import sanitize_error, sanitize_payload
from .source_fence import before_fences
from .target_admission import contract_for, read_target_view
from .target_contract import TargetContract, mismatches, preserved_identity_config

logger = logging.getLogger(__name__)


def build_live_contract(
    source_client: Any,
    target_client: Any,
    *,
    job: MigrationJob,
    request: MigrationPlanCreateRequest,
    root_pool_name: Optional[str],
    source_name: str,
    config_overrides: Optional[dict[str, Any]] = None,
    source_state: Optional[dict[str, Any]] = None,
) -> tuple[TargetContract, dict[str, Any]]:
    """Re-derive the approved target contract from current daemon state (blocking)."""
    source = executor.read_instance(source_client, project=job.source_project, name=source_name)
    if source is None:
        raise MigrationAuthorityError(
            f"Source instance '{source_name}' disappeared.", code="source_missing"
        )
    source = before_fences(source, source_state)
    mappings = request.mappings.model_dump(mode="json")
    profile_mapping = dict(mappings.get("profiles") or {})
    view = read_target_view(
        target_client=target_client,
        target_project=job.target_project,
        target_project_record=preflight._get_project(target_client, job.target_project),
        source_project_record=preflight._get_project(source_client, job.source_project),
        profile_names=[
            str(profile_mapping.get(name, name)) for name in (source.get("profiles") or [])
        ],
    )
    contract = contract_for(
        source=source,
        view=view,
        mappings=mappings,
        root_pool=root_pool_name,
        intent=job.intent,
        config_overrides=config_overrides,
    )
    return contract, source


def _approved_contract_hash(resource: MigrationResource) -> Optional[str]:
    contract = (resource.target_ref or {}).get("contract")
    value = contract.get("contract_hash") if isinstance(contract, dict) else None
    return value if isinstance(value, str) and value else None


async def _verify_snapshots(
    job: MigrationJob,
    source_client: Any,
    target_client: Any,
    *,
    source_name: str,
    target_name: str,
    expected_hash: Any,
    label: str,
) -> str:
    if not isinstance(expected_hash, str) or not expected_hash:
        raise MigrationUnknown(
            f"Snapshot inventory for '{source_name}' was not pinned before submission."
        )
    target_hash = await _blocking(
        executor.snapshot_inventory_fingerprint,
        target_client,
        project=job.target_project,
        name=target_name,
        label=f"migration.{label}_target_snapshots[{target_name}]",
    )
    if target_hash != expected_hash:
        raise MigrationUnknown(
            f"Target '{target_name}' does not have the approved source snapshot inventory."
        )
    return target_hash


async def _pin_source(
    job: MigrationJob, source_client: Any, *, source_name: str, state: dict[str, Any]
) -> dict[str, Any]:
    """The source must be the approved, stopped object with its pinned definition."""
    source = await _blocking(
        executor.read_instance,
        source_client,
        project=job.source_project,
        name=source_name,
        label=f"migration.pin_source[{source_name}]",
    )
    if (
        source is None
        or executor.instance_identity_fingerprint(source) != state.get("source_identity_hash")
        or executor.instance_fingerprint(source) != state.get("source_definition_hash")
        or str(source.get("status") or "").lower() != "stopped"
    ):
        raise MigrationAuthorityError(
            f"Source '{source_name}' no longer matches its approved stopped identity.",
            code="source_fence_changed",
        )
    return source


async def _submit_staged(
    job: MigrationJob,
    resource: MigrationResource,
    state: dict[str, Any],
    source_client: Any,
    target_client: Any,
    *,
    source_name: str,
    target_name: str,
    definition: dict[str, Any],
) -> tuple[Optional[str], Any, str, Optional[str]]:
    """Submit one copy or pull. Returns (operation, client, project, source operation)."""
    if job.source_cluster_id == job.target_cluster_id:
        await _assert_mutation_lease()
        try:
            _transport, operation_id, operation_client = await _blocking(
                executor.submit_copy_instance,
                source_client,
                target_client,
                source_cluster=job.source_cluster_id,
                source_project=job.source_project,
                source_name=source_name,
                target_cluster=job.target_cluster_id,
                target_project=job.target_project,
                target_name=target_name,
                target_member=job.target_member,
                target_pool=None,
                definition=definition,
                label=f"migration.transfer[{source_name}]",
            )
        except Exception as exc:  # noqa: BLE001
            if executor.is_definitive_rejection(exc):
                detail = sanitize_error(exc, limit=300)
                logger.warning("Migration %s copy of %s was refused: %s", job.job_id, source_name, detail)
                raise TransferFailed(
                    f"The cluster refused the copy of '{source_name}': {detail}"
                ) from exc
            raise MigrationUnknown(
                f"Transfer submission for '{source_name}' has an unknown outcome."
            ) from exc
        return operation_id, operation_client, job.target_project, None

    state.update(
        {
            "source_submission_intent": True,
            "source_submission_at": repository.utcnow().isoformat(),
        }
    )
    await _resource_update(resource.resource_id, state="syncing", safe_result=state)
    await _assert_mutation_lease()
    try:
        handoff, source_operation_id = await _blocking(
            executor.prepare_cross_cluster_source,
            source_client,
            source_project=job.source_project,
            source_name=source_name,
            label=f"migration.prepare_source_transfer[{source_name}]",
        )
    except Exception as exc:  # noqa: BLE001
        if executor.is_definitive_rejection(exc):
            raise TransferFailed(
                f"The source cluster refused to start migrating '{source_name}'."
            ) from exc
        raise MigrationUnknown("The source transfer submission outcome is unknown.") from exc
    if source_operation_id:
        await _resource_update(
            resource.resource_id,
            state="syncing",
            operation_ref={
                "kind": "source_transfer",
                "cluster_id": job.source_cluster_id,
                "project": job.source_project,
                "operation_id": source_operation_id,
            },
        )
    state.update(
        {
            "target_submission_intent": True,
            "target_submission_at": repository.utcnow().isoformat(),
        }
    )
    await _resource_update(resource.resource_id, state="syncing", safe_result=state)
    await _assert_mutation_lease()
    try:
        operation_id = await _blocking(
            executor.submit_cross_cluster_target,
            target_client,
            target_project=job.target_project,
            target_name=target_name,
            target_member=job.target_member,
            target_pool=None,
            handoff=handoff,
            definition=definition,
            label=f"migration.accept_target_transfer[{source_name}]",
        )
    except Exception as exc:  # noqa: BLE001
        if executor.is_definitive_rejection(exc):
            raise TransferFailed(
                f"The target cluster refused the migration of '{source_name}'."
            ) from exc
        raise MigrationUnknown("The target transfer submission outcome is unknown.") from exc
    return operation_id, target_client, job.target_project, source_operation_id


async def _transfer_staged(
    job: MigrationJob,
    resource: MigrationResource,
    state: dict[str, Any],
    source_client: Any,
    target_client: Any,
    *,
    request: MigrationPlanCreateRequest,
    root_pool_name: Optional[str],
) -> tuple[dict[str, Any], TargetContract]:
    source_name = str((resource.source_ref or {}).get("name") or "")
    target_name = str((resource.target_ref or {}).get("name") or source_name)
    await _pin_source(job, source_client, source_name=source_name, state=state)
    contract, source = await _blocking(
        build_live_contract,
        source_client,
        target_client,
        job=job,
        request=request,
        root_pool_name=root_pool_name,
        source_name=source_name,
        label=f"migration.target_contract[{source_name}]",
    )
    approved = _approved_contract_hash(resource)
    if approved is None or contract.contract_hash != approved:
        raise MigrationAuthorityError(
            f"The target definition for '{source_name}' changed after approval.",
            code="target_contract_drift",
        )
    definition = contract.definition(
        instance_type=str(source.get("type") or "container"),
        architecture=source.get("architecture"),
        job_id=job.job_id,
        preserved=preserved_identity_config(source, contract, intent=job.intent),
    )
    state.update(
        {
            "contract_hash": contract.contract_hash,
            "transfer_bytes": state.get("transfer_bytes"),
            "transfer_submission_intent": True,
            "transfer_submission_at": repository.utcnow().isoformat(),
            "target_started": False,
            "target_start_attempted": False,
        }
    )
    await _resource_update(resource.resource_id, state="syncing", safe_result=state)
    operation_id, operation_client, operation_project, source_operation_id = await _submit_staged(
        job,
        resource,
        state,
        source_client,
        target_client,
        source_name=source_name,
        target_name=target_name,
        definition=definition,
    )
    if operation_id:
        await _resource_update(
            resource.resource_id,
            state="syncing",
            operation_ref={
                "kind": "transfer",
                "cluster_id": job.target_cluster_id,
                "project": operation_project,
                "operation_id": operation_id,
            },
        )

    async def progress(elapsed: int) -> None:
        await _resource_update(
            resource.resource_id,
            state="syncing",
            progress={"mode": "elapsed", "elapsed_seconds": elapsed},
        )

    try:
        outcome = await await_operation(
            operation_client,
            operation_id=operation_id,
            project=operation_project,
            label=f"migration.wait_transfer[{source_name}]",
            deadline_seconds=transfer_deadline_seconds(state.get("transfer_bytes")),
            on_progress=progress,
        )
    except executor.OperationForgotten:
        # The daemon finished and purged the operation. Readback below decides the outcome.
        outcome = {"settled": True, "failed": False}
    if outcome.get("failed"):
        await _resource_update(
            resource.resource_id,
            state="failed",
            safe_result=state,
            error_code="daemon_operation_failed",
            error_message=outcome.get("err") or "Migration operation failed.",
        )
        raise TransferFailed(outcome.get("err") or f"Transfer for '{source_name}' failed.")
    if source_operation_id:
        try:
            source_outcome = await await_operation(
                source_client,
                operation_id=source_operation_id,
                project=job.source_project,
                label=f"migration.wait_source_transfer[{source_name}]",
                deadline_seconds=600,
            )
        except executor.OperationForgotten:
            source_outcome = {"settled": True, "failed": False}
        if source_outcome.get("failed"):
            raise TransferFailed(
                source_outcome.get("err") or f"Source transfer for '{source_name}' failed."
            )

    target = await _blocking(
        executor.read_instance,
        target_client,
        project=job.target_project,
        name=target_name,
        label=f"migration.validate_target[{target_name}]",
    )
    if target is None:
        raise MigrationUnknown(f"Transfer settled but target '{target_name}' is absent.")
    target_identity = executor.instance_identity_fingerprint(target)
    problems = mismatches(target, contract)
    if (
        not target_identity
        or str(target.get("status") or "").lower() != "stopped"
        or (target.get("config") or {}).get(MIGRATION_JOB_MARKER) != job.job_id
        or problems
    ):
        raise MigrationUnknown(
            f"Target '{target_name}' does not match its approved definition "
            f"({', '.join(problems[:6]) or 'identity or state'})."
        )
    if job.target_member and str(target.get("location") or "") != job.target_member:
        raise MigrationUnknown(f"Target '{target_name}' is not on the approved member.")
    snapshots = await _verify_snapshots(
        job,
        source_client,
        target_client,
        source_name=source_name,
        target_name=target_name,
        expected_hash=state.get("source_snapshot_inventory_hash"),
        label="validate",
    )
    state.update(
        {
            "target_identity_hash": target_identity,
            "target_definition_hash": executor.instance_fingerprint(target),
            "target_snapshot_inventory_hash": snapshots,
        }
    )
    await _resource_update(resource.resource_id, state="ready", safe_result=state)
    return state, contract


async def _transfer_member(
    job: MigrationJob,
    resource: MigrationResource,
    state: dict[str, Any],
    source_client: Any,
    *,
    target_pool: Optional[str],
) -> dict[str, Any]:
    source_name = str((resource.source_ref or {}).get("name") or "")
    source_root = state.get("source_root_pool")
    if not isinstance(source_root, str) or not source_root or (
        target_pool is not None and target_pool != source_root
    ):
        raise RuntimeError(
            "Member relocation root-pool evidence does not match the approved mapping."
        )
    state.update(
        {
            "transfer_submission_intent": True,
            "transfer_submission_at": repository.utcnow().isoformat(),
        }
    )
    await _resource_update(resource.resource_id, state="syncing", safe_result=state)
    await _assert_mutation_lease()
    try:
        operation_id = await _blocking(
            executor.submit_member_relocation,
            source_client,
            project=job.source_project,
            name=source_name,
            target_member=str(job.target_member),
            label=f"migration.member_relocation[{source_name}]",
        )
    except Exception as exc:  # noqa: BLE001
        if executor.is_definitive_rejection(exc):
            raise TransferFailed(f"The cluster refused to relocate '{source_name}'.") from exc
        raise MigrationUnknown(
            f"Member relocation submission for '{source_name}' is unknown."
        ) from exc
    if operation_id:
        await _resource_update(
            resource.resource_id,
            state="syncing",
            operation_ref={
                "kind": "transfer",
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
            label=f"migration.wait_relocation[{source_name}]",
            deadline_seconds=transfer_deadline_seconds(state.get("transfer_bytes")),
        )
    except executor.OperationForgotten:
        outcome = {"settled": True, "failed": False}
    if outcome.get("failed"):
        raise TransferFailed(outcome.get("err") or f"Relocation of '{source_name}' failed.")
    relocated = await _blocking(
        executor.read_instance,
        source_client,
        project=job.source_project,
        name=source_name,
        label=f"migration.validate_relocation[{source_name}]",
    )
    if (
        relocated is None
        or executor.instance_identity_fingerprint(relocated) != state.get("source_identity_hash")
        or str(relocated.get("status") or "").lower() != "stopped"
        or str(relocated.get("location") or "") != str(job.target_member or "")
        or root_pool(relocated) != source_root
    ):
        raise MigrationUnknown(
            f"Member relocation settled but '{source_name}' is not on the approved member."
        )
    state.update(
        {
            "target_identity_hash": state.get("source_identity_hash"),
            "target_definition_hash": executor.instance_fingerprint(relocated),
        }
    )
    await _resource_update(resource.resource_id, state="ready", safe_result=state)
    return state


async def _transfer_resources(
    job: MigrationJob,
    worker_id: str,
    source_client: Any,
    target_client: Any,
    resources: list[MigrationResource],
    prepared: dict[str, dict[str, Any]],
    *,
    request: MigrationPlanCreateRequest,
    root_pool_name: Optional[str],
) -> list[tuple[MigrationResource, dict[str, Any], Optional[TargetContract]]]:
    transferred: list[tuple[MigrationResource, dict[str, Any], Optional[TargetContract]]] = []
    for resource in resources:
        if resource.resource_type != "instance":
            continue
        state = dict(prepared.get(resource.resource_id) or resource.safe_result or {})
        if job.strategy == "member_relocation":
            state = await _transfer_member(
                job, resource, state, source_client, target_pool=_target_pool(request)
            )
            contract = None
        else:
            state, contract = await _transfer_staged(
                job,
                resource,
                state,
                source_client,
                target_client,
                request=request,
                root_pool_name=root_pool_name,
            )
        prepared[resource.resource_id] = state
        transferred.append((resource, state, contract))
    return transferred


async def _admit_worker_run(
    job: MigrationJob, worker_id: str, source_client: Any, report: dict[str, Any]
) -> None:
    job_state = dict(job.safe_result or {})
    reason = str(job_state.get("reason") or "")
    acknowledgements = [
        str(value) for value in (job_state.get("acknowledgements") or []) if isinstance(value, str)
    ]
    validate_acknowledgements(
        intent=job.intent,
        strategy=job.strategy,
        external_cutover_required=job.external_cutover_required,
        acknowledgements=acknowledgements,
        source_running=report_has_running_source(report),
        unmeasured_size=report_needs_unmeasured_size_consent(report),
    )
    billing_impact_count = await _blocking(
        enforce_migration_billing_policy,
        source_client,
        project=job.source_project,
        report=report,
        reason=reason,
        acknowledgements=acknowledgements,
        label="migration.fresh_billing_policy",
    )
    if billing_impact_count and not job_state.get("billing_override_audited"):
        job_state = {
            **job_state,
            "billing_override_audited": True,
            "billing_impact_workload_count": billing_impact_count,
        }
        await _fenced_job_update(
            job,
            worker_id,
            phase=job.phase,
            status="running",
            safe_result=sanitize_payload(job_state),
            outbox_events=[
                billing_override_event(
                    job.job_id,
                    workload_count=billing_impact_count,
                    reason=reason,
                    phase=job.phase,
                )
            ],
        )


def untouched(resources: list[Any]) -> bool:
    """No resource recorded a daemon submission or left its first state: nothing was changed."""
    return all(
        not resource.operation_refs and resource.state in {"pending", "blocked"}
        for resource in resources
    )


async def admit_untouched_run(
    job: MigrationJob, worker_id: str, source_client: Any, report: dict[str, Any]
) -> None:
    """`_admit_worker_run` for a job that changed nothing yet: a refusal ends the job cleanly.

    note: for example a dir-pool target that filled up after approval now needs a consent the
    operator never gave. Waiting cannot fix that, and manual recovery would hold the slot.
    """
    try:
        await _admit_worker_run(job, worker_id, source_client, report)
    except MigrationConflict as exc:
        raise MigrationPreMutationRefusal(str(exc), code="acknowledgement_required") from exc


async def _perform_copy_or_cutover(
    job: MigrationJob,
    worker_id: str,
    source_client: Any,
    target_client: Any,
) -> None:
    plan = await _job_plan(job)
    request = MigrationPlanCreateRequest.model_validate(plan.request_payload)
    resources = await _resources(job.job_id)
    report = await _fresh_worker_preflight(job, source_client, target_client)
    queued = job.phase == "queued"
    if report["blockers"]:
        if queued:
            raise MigrationPreMutationRefusal(
                "Fresh preflight found blockers before any change.", code="preflight_blocked"
            )
        raise RuntimeError("Fresh cutover preflight found blockers or inventory drift.")
    if queued:
        await admit_untouched_run(job, worker_id, source_client, report)
    else:
        await _admit_worker_run(job, worker_id, source_client, report)
    await _fenced_job_update(
        job,
        worker_id,
        phase="preparing_target",
        status="running",
        event_message="Preparing and validating the migration target.",
    )
    created_target_project = await _ensure_project_target(
        job, worker_id, source_client, target_client
    )
    if created_target_project:
        post_create_report = await _fresh_worker_preflight(job, source_client, target_client)
        if post_create_report["blockers"]:
            raise MigrationUnknown(
                "Target project exists but post-create compatibility validation failed."
            )
    prepared: dict[str, dict[str, Any]] = {}
    root_pool_name = approved_root_pool(plan.preflight_payload or {}) or _target_pool(request)
    try:
        await _fenced_job_update(
            job,
            worker_id,
            phase="quiescing",
            status="running",
            event_message="Fencing the source workloads for a cold transfer.",
        )
        prepared = await _prepare_sources(
            job, worker_id, source_client, resources, plan.checkpoint_requested
        )
        await _fenced_job_update(
            job,
            worker_id,
            phase="initial_sync" if job.intent == "copy" else "final_sync",
            status="running",
            event_message="Transferring workload data to the target.",
        )
        transferred = await _transfer_resources(
            job,
            worker_id,
            source_client,
            target_client,
            resources,
            prepared,
            request=request,
            root_pool_name=root_pool_name,
        )
    except (TransferFailed, MigrationAuthorityError) as exc:
        await abort_before_activation(
            job, worker_id, source_client, target_client, reason=exc
        )
        return
    await finish_transfer(job, worker_id, source_client, target_client, transferred)


async def resume_after_transfer(
    job: MigrationJob,
    worker_id: str,
    source_client: Any,
    target_client: Any,
) -> None:
    """Continue at activation once every target was transferred and adopted."""
    plan = await _job_plan(job)
    request = MigrationPlanCreateRequest.model_validate(plan.request_payload)
    root_pool_name = approved_root_pool(plan.preflight_payload or {}) or _target_pool(request)
    transferred: list[tuple[MigrationResource, dict[str, Any], Optional[TargetContract]]] = []
    for resource in await _resources(job.job_id):
        if resource.resource_type != "instance":
            continue
        state = dict(resource.safe_result or {})
        if not state.get("target_identity_hash"):
            raise MigrationUnknown("A transferred target has no adopted identity yet.")
        contract: Optional[TargetContract] = None
        if job.strategy != "member_relocation":
            source_name = str((resource.source_ref or {}).get("name") or "")
            contract, _source = await _blocking(
                build_live_contract,
                source_client,
                target_client,
                job=job,
                request=request,
                root_pool_name=root_pool_name,
                source_name=source_name,
                source_state=state,
                label=f"migration.resume_contract[{source_name}]",
            )
            if contract.contract_hash != state.get("contract_hash"):
                raise MigrationAuthorityError(
                    f"The target definition for '{source_name}' changed after transfer.",
                    code="target_contract_drift",
                )
        transferred.append((resource, state, contract))
    await finish_transfer(job, worker_id, source_client, target_client, transferred)


__all__ = [
    "_perform_copy_or_cutover",
    "_transfer_resources",
    "build_live_contract",
    "resume_after_transfer",
]
