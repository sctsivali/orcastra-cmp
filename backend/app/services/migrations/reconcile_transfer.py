"""Reconcile a staged copy or move whose transfer outcome was not observed.

LXD purges finished operations within seconds, so an operation id is weak evidence. A staged
target instead carries this job's id in its config (``user.orcastra.migration-job``), which no
other request can produce. A target with that marker, the approved contract, the pinned snapshot
inventory, and a stopped never-started state is adopted. A transfer that is still running is
watched until it settles. A transfer that failed, or left no target while nothing is running,
goes to the abort runner, which undoes only what this job created.
"""

from __future__ import annotations

from typing import Any

from app.models.migration_models import MigrationJob
from app.models.migration_schemas import MigrationPlanCreateRequest

from . import executor
from .context import _fenced_job_update, _job_plan, _resource_update, _resources
from .contracts import MigrationAuthorityError, MigrationUnknown, _blocking, logger
from .operation_wait import await_operation, transfer_deadline_seconds
from .plan_contracts import _target_pool, approved_root_pool
from .project_policy import MIGRATION_JOB_MARKER
from .security import sanitize_error, sanitize_payload
from .target_contract import mismatches


async def _watch_transfer(job: MigrationJob, resource: Any, target_client: Any) -> str:
    """Return 'settled', 'failed', or 'absent' for the recorded transfer operation."""
    refs = [
        ref
        for ref in (resource.operation_refs or [])
        if ref.get("kind") == "transfer" and ref.get("operation_id")
    ]
    if not refs:
        return "absent"
    ref = refs[-1]
    try:
        outcome = await await_operation(
            target_client,
            operation_id=str(ref["operation_id"]),
            project=str(ref.get("project") or job.target_project),
            label=f"migration.reconcile_watch[{ref['operation_id']}]",
            deadline_seconds=transfer_deadline_seconds(
                (resource.safe_result or {}).get("transfer_bytes")
            ),
        )
    except executor.OperationForgotten:
        return "absent"
    return "failed" if outcome.get("failed") else "settled"


async def reconcile_staged(
    job: MigrationJob,
    worker_id: str,
    source_client: Any,
    target_client: Any,
    *,
    resources: list[Any] | None = None,
) -> bool:
    """Handle a staged job with transfer intent. Returns False when not applicable."""
    if job.scope != "instance" or job.strategy == "member_relocation":
        return False
    if resources is None:
        resources = await _resources(job.job_id)
    instances = [resource for resource in resources if resource.resource_type == "instance"]
    if not any(
        (resource.safe_result or {}).get(key)
        for resource in instances
        for key in ("transfer_submission_intent", "target_submission_intent")
    ):
        return False
    # Only jobs that submitted against an approved contract stamp their targets with the job
    # marker. Jobs without a contract hash keep the operation-id based reconciliation.
    if not any((resource.safe_result or {}).get("contract_hash") for resource in instances):
        return False
    from .transfer import build_live_contract

    plan = await _job_plan(job)
    request = MigrationPlanCreateRequest.model_validate(plan.request_payload)
    root_pool_name = approved_root_pool(plan.preflight_payload or {}) or _target_pool(request)
    abort = False
    adopted = False
    for resource in instances:
        state = dict(resource.safe_result or {})
        if state.get("target_identity_hash") and state.get("contract_hash"):
            continue
        source_name = str((resource.source_ref or {}).get("name") or "")
        target_name = str((resource.target_ref or {}).get("name") or source_name)
        watched = await _watch_transfer(job, resource, target_client)
        target = await _blocking(
            executor.read_instance,
            target_client,
            project=job.target_project,
            name=target_name,
            label=f"migration.reconcile_staged_target[{target_name}]",
        )
        if target is None:
            if watched == "settled":
                raise MigrationUnknown(f"Transfer settled but '{target_name}' is absent.")
            abort = True
            continue
        if (target.get("config") or {}).get(MIGRATION_JOB_MARKER) != job.job_id:
            raise MigrationAuthorityError(
                f"Target '{target_name}' exists but was not created by this migration.",
                code="target_not_owned",
            )
        if watched == "failed" or str(target.get("status") or "").lower() != "stopped":
            abort = watched == "failed"
            if not abort:
                raise MigrationUnknown(f"Target '{target_name}' is not in its stopped state.")
            continue
        contract, _source = await _blocking(
            build_live_contract,
            source_client,
            target_client,
            job=job,
            request=request,
            root_pool_name=root_pool_name,
            source_name=source_name,
            label=f"migration.reconcile_contract[{source_name}]",
        )
        snapshots = await _blocking(
            executor.snapshot_inventory_fingerprint,
            target_client,
            project=job.target_project,
            name=target_name,
            label=f"migration.reconcile_snapshots[{target_name}]",
        )
        if mismatches(target, contract) or snapshots != state.get(
            "source_snapshot_inventory_hash"
        ):
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
        adopted = True

    if abort:
        logger.info("Migration %s transfer failed or left no target; aborting.", job.job_id)
        await _fenced_job_update(
            job,
            worker_id,
            phase="rolling_back",
            status="running",
            locked_by=None,
            lease_expires_at=None,
            safe_result=sanitize_payload(
                {
                    **(job.safe_result or {}),
                    "recovery_kind": "abort",
                    "abort_code": "transfer_failed",
                    "abort_reason": sanitize_error("The transfer failed or left no target."),
                }
            ),
            event_message="Reconciliation found a failed transfer. Undoing this job's changes.",
        )
        return True
    await _fenced_job_update(
        job,
        worker_id,
        phase="target_validating",
        status="running",
        locked_by=None,
        lease_expires_at=None,
        error_code=None,
        error_message=None,
        # important: only new adoption is progress. Resetting the streak when every target was
        # adopted before would count a start that keeps failing as a first attempt forever.
        safe_result=sanitize_payload(
            {**(job.safe_result or {}), "reconciliation_streak": 0} if adopted else dict(job.safe_result or {})
        ),
        event_message="Reconciliation adopted the transferred targets. Resuming activation.",
    )
    return True


__all__ = ["reconcile_staged"]
