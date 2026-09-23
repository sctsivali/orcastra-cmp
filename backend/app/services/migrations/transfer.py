"""Validated workload transfer and cutover execution."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

from app.models.migration_models import MigrationJob, MigrationResource
from app.models.migration_schemas import MigrationPlanCreateRequest

from . import executor, repository
from .billing import (
    billing_override_event,
    enforce_migration_billing_policy,
    validate_acknowledgements,
)
from .completion import target_completion_events
from .context import (
    _assert_mutation_lease,
    _fenced_job_update,
    _fresh_worker_preflight,
    _job_plan,
    _resource_update,
    _resources,
)
from .contracts import MigrationAuthorityError, MigrationUnknown, _blocking
from .plan_contracts import _target_pool
from .preparation import (
    _ensure_project_target,
    _prepare_sources,
    _restart_source_best_effort,
)
from .security import sanitize_payload


def _root_pool(instance: dict[str, Any]) -> str | None:
    devices = instance.get("expanded_devices") or instance.get("devices") or {}
    roots = [
        device
        for device in devices.values()
        if isinstance(device, dict)
        and device.get("type") == "disk"
        and device.get("path") == "/"
    ] if isinstance(devices, dict) else []
    if len(roots) != 1 or not isinstance(roots[0].get("pool"), str):
        return None
    return str(roots[0].get("pool") or "") or None


async def _verify_copy_snapshots(
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
            f"Copy snapshot inventory for '{source_name}' was not pinned before submission."
        )
    source_hash = await _blocking(
        executor.snapshot_inventory_fingerprint,
        source_client,
        project=job.source_project,
        name=source_name,
        label=f"migration.{label}_source_snapshots[{source_name}]",
    )
    target_hash = await _blocking(
        executor.snapshot_inventory_fingerprint,
        target_client,
        project=job.target_project,
        name=target_name,
        label=f"migration.{label}_target_snapshots[{target_name}]",
    )
    if source_hash != expected_hash or target_hash != expected_hash:
        raise MigrationUnknown(
            f"Target '{target_name}' does not have the approved source snapshot inventory."
        )
    return target_hash


async def _transfer_resources(
    job: MigrationJob,
    worker_id: str,
    source_client: Any,
    target_client: Any,
    resources: list[MigrationResource],
    prepared: dict[str, dict[str, Any]],
    target_pool: Optional[str],
) -> None:
    transferred: list[tuple[MigrationResource, str, dict[str, Any]]] = []
    for resource in resources:
        if resource.resource_type != "instance":
            continue
        source_name = str((resource.source_ref or {}).get("name") or "")
        target_name = str((resource.target_ref or {}).get("name") or source_name)
        state = dict(prepared.get(resource.resource_id) or resource.safe_result or {})
        if job.intent == "copy":
            source_record = await _blocking(
                executor.read_instance,
                source_client,
                project=job.source_project,
                name=source_name,
                label=f"migration.pin_copy_source[{source_name}]",
            )
            approved_identity = (resource.source_ref or {}).get(
                "identity_fingerprint"
            )
            if (
                source_record is None
                or not isinstance(approved_identity, str)
                or not approved_identity
                or executor.instance_identity_fingerprint(source_record)
                != approved_identity
                or str(source_record.get("status") or "").lower() != "stopped"
            ):
                raise MigrationAuthorityError(
                    f"Copy source '{source_name}' no longer matches its approved stopped identity.",
                    code="copy_source_fence_changed",
                )
            snapshot_hash = await _blocking(
                executor.snapshot_inventory_fingerprint,
                source_client,
                project=job.source_project,
                name=source_name,
                label=f"migration.pin_copy_source_snapshots[{source_name}]",
            )
            state.update(
                {
                    "original_status": "stopped",
                    "source_definition_hash": executor.instance_fingerprint(
                        source_record
                    ),
                    "source_identity_hash": approved_identity,
                    "source_snapshot_inventory_hash": snapshot_hash,
                    "target_started": False,
                    "target_start_attempted": False,
                }
            )
            prepared[resource.resource_id] = state
            await _resource_update(
                resource.resource_id,
                state="preparing",
                safe_result=state,
            )
        if job.strategy == "member_relocation":
            source_root_pool = state.get("source_root_pool")
            if (
                not isinstance(source_root_pool, str)
                or not source_root_pool
                or (target_pool is not None and target_pool != source_root_pool)
            ):
                raise RuntimeError(
                    "Member relocation root-pool evidence does not match the approved mapping."
                )
        state = {
            **state,
            "transfer_submission_intent": True,
            "transfer_submission_at": repository.utcnow().isoformat(),
        }
        prepared[resource.resource_id] = state
        await _resource_update(
            resource.resource_id, state="syncing", safe_result=state
        )
        if job.strategy == "member_relocation":
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
                raise MigrationUnknown(
                    f"Member relocation submission for '{source_name}' is unknown."
                ) from exc
            operation_cluster = job.source_cluster_id
            operation_client = source_client
            operation_project = job.source_project
        elif job.source_cluster_id != job.target_cluster_id:
            state = {
                **state,
                "source_submission_intent": True,
                "source_submission_at": repository.utcnow().isoformat(),
            }
            prepared[resource.resource_id] = state
            await _resource_update(
                resource.resource_id, state="syncing", safe_result=state
            )
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
                raise MigrationUnknown(
                    "The source transfer submission outcome is unknown."
                ) from exc
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
            state = {
                **state,
                "target_submission_intent": True,
                "target_submission_at": repository.utcnow().isoformat(),
            }
            prepared[resource.resource_id] = state
            await _resource_update(
                resource.resource_id, state="syncing", safe_result=state
            )
            await _assert_mutation_lease()
            try:
                operation_id = await _blocking(
                    executor.submit_cross_cluster_target,
                    target_client,
                    target_project=job.target_project,
                    target_name=target_name,
                    target_member=job.target_member,
                    target_pool=target_pool,
                    handoff=handoff,
                    label=f"migration.accept_target_transfer[{source_name}]",
                )
            except Exception as exc:  # noqa: BLE001
                raise MigrationUnknown(
                    "The target transfer submission outcome is unknown."
                ) from exc
            operation_cluster = job.target_cluster_id
            operation_client = target_client
            operation_project = job.target_project
        else:
            await _assert_mutation_lease()
            try:
                _, operation_id, operation_client = await _blocking(
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
                    target_pool=target_pool,
                    label=f"migration.transfer[{source_name}]",
                )
            except Exception as exc:  # noqa: BLE001
                raise MigrationUnknown(
                    f"Transfer submission for '{source_name}' has an unknown outcome."
                ) from exc
            operation_cluster = job.target_cluster_id
            operation_project = job.target_project
        operation_ref = (
            {
                "kind": "transfer",
                "cluster_id": operation_cluster,
                "project": operation_project,
                "operation_id": operation_id,
            }
            if operation_id
            else None
        )
        if operation_ref:
            await _resource_update(
                resource.resource_id, state="syncing", operation_ref=operation_ref
            )
        outcome = await _blocking(
            executor.wait_operation,
            operation_client,
            operation_id=operation_id,
            project=operation_project,
            timeout=20,
            label=f"migration.wait_transfer[{source_name}]",
        )
        if not outcome.get("settled"):
            await _resource_update(resource.resource_id, state="unknown")
            raise MigrationUnknown(
                f"Transfer for '{source_name}' is still running and needs reconciliation."
            )
        if outcome.get("failed"):
            await _resource_update(
                resource.resource_id,
                state="failed",
                error_code="daemon_operation_failed",
                error_message=outcome.get("err") or "Migration operation failed.",
            )
            raise RuntimeError(outcome.get("err") or f"Transfer for '{source_name}' failed.")
        if job.source_cluster_id != job.target_cluster_id and source_operation_id:
            source_outcome = await _blocking(
                executor.wait_operation,
                source_client,
                operation_id=source_operation_id,
                project=job.source_project,
                timeout=20,
                label=f"migration.wait_source_transfer[{source_name}]",
            )
            if not source_outcome.get("settled"):
                raise MigrationUnknown(
                    f"Source transfer for '{source_name}' still needs reconciliation."
                )
            if source_outcome.get("failed"):
                raise RuntimeError(
                    source_outcome.get("err") or f"Source transfer for '{source_name}' failed."
                )
        target_record = await _blocking(
            executor.read_instance,
            target_client,
            project=job.target_project,
            name=target_name,
            label=f"migration.validate_target[{target_name}]",
        )
        if target_record is None:
            raise MigrationUnknown(
                f"Transfer operation settled but target '{target_name}' is absent."
            )
        target_identity_hash = executor.instance_identity_fingerprint(target_record)
        if not isinstance(target_identity_hash, str) or not target_identity_hash:
            raise MigrationUnknown(
                f"Target '{target_name}' has no authoritative immutable identity."
            )
        if str(target_record.get("status") or "").lower() != "stopped":
            raise MigrationUnknown(
                f"Target '{target_name}' is not in the approved stopped state."
            )
        target_snapshot_hash: str | None = None
        if job.strategy == "member_relocation":
            if str(target_record.get("location") or "") != str(job.target_member or ""):
                raise MigrationUnknown(
                    f"Member relocation settled but '{target_name}' is not on the approved member."
                )
            if _root_pool(target_record) != state.get("source_root_pool"):
                raise MigrationUnknown(
                    f"Member relocation changed the root pool for '{target_name}'."
                )
            source_record = target_record
        else:
            source_record = await _blocking(
                executor.read_instance,
                source_client,
                project=job.source_project,
                name=source_name,
                label=f"migration.validate_source[{source_name}]",
            )
            if source_record is None or not executor.instances_equivalent(
                source_record, target_record
            ):
                raise MigrationUnknown(
                    f"Target '{target_name}' does not match the authoritative source definition."
                )
            if job.intent == "copy" and (
                executor.instance_identity_fingerprint(source_record)
                != state.get("source_identity_hash")
                or executor.instance_fingerprint(source_record)
                != state.get("source_definition_hash")
                or str(source_record.get("status") or "").lower() != "stopped"
            ):
                raise MigrationUnknown(
                    f"Copy source '{source_name}' changed after submission."
                )
            if (
                job.intent == "move"
                and str(source_record.get("status") or "").lower() != "stopped"
            ):
                raise MigrationUnknown(
                    f"Source '{source_name}' is not stopped after transfer."
                )
            if job.intent == "copy":
                target_snapshot_hash = await _verify_copy_snapshots(
                    job,
                    source_client,
                    target_client,
                    source_name=source_name,
                    target_name=target_name,
                    expected_hash=state.get("source_snapshot_inventory_hash"),
                    label="validate_copy",
                )
        state = prepared.get(resource.resource_id) or resource.safe_result or {}
        state = {
            **state,
            "target_started": False,
            "target_start_attempted": False,
            "target_definition_hash": executor.instance_fingerprint(target_record),
            "target_identity_hash": target_identity_hash,
            "target_snapshot_inventory_hash": target_snapshot_hash,
        }
        prepared[resource.resource_id] = state
        transferred.append((resource, target_name, state))
        await _resource_update(
            resource.resource_id,
            state="ready",
            safe_result=state,
        )

    # Validate every target before starting any one of them. Starting while later transfers are
    # still unverified creates a split-brain failure path when a subsequent resource fails.
    for resource, target_name, state in transferred:
        source_name = str((resource.source_ref or {}).get("name") or "")
        target_record = await _blocking(
            executor.read_instance,
            target_client,
            project=job.target_project,
            name=target_name,
            label=f"migration.validation_barrier[{target_name}]",
        )
        if target_record is None:
            raise MigrationUnknown(f"Target '{target_name}' disappeared before activation.")
        if str(target_record.get("status") or "").lower() != "stopped":
            raise MigrationUnknown(
                f"Target '{target_name}' left its stopped state before the cutover barrier."
            )
        current_identity = executor.instance_identity_fingerprint(target_record)
        if state.get("target_identity_hash") and (
            current_identity != state["target_identity_hash"]
        ):
            raise MigrationUnknown(
                f"Target '{target_name}' was replaced during cutover validation."
            )
        if job.strategy == "member_relocation":
            if str(target_record.get("location") or "") != str(job.target_member or ""):
                raise MigrationUnknown(
                    f"Target '{target_name}' left the approved cluster member before activation."
                )
            if _root_pool(target_record) != state.get("source_root_pool"):
                raise MigrationUnknown(
                    f"Target '{target_name}' changed root pool before activation."
                )
        else:
            source_record = await _blocking(
                executor.read_instance,
                source_client,
                project=job.source_project,
                name=source_name,
                label=f"migration.validation_source_barrier[{source_name}]",
            )
            if source_record is None or not executor.instances_equivalent(
                source_record, target_record
            ):
                raise MigrationUnknown(
                    f"Target '{target_name}' drifted from the source before activation."
                )
            if (
                job.intent == "move"
                and str(source_record.get("status") or "").lower() != "stopped"
            ):
                raise MigrationUnknown(
                    f"Source '{source_name}' crossed its stopped cutover fence."
                )
            if job.intent == "copy":
                if (
                    executor.instance_identity_fingerprint(source_record)
                    != state.get("source_identity_hash")
                    or executor.instance_fingerprint(source_record)
                    != state.get("source_definition_hash")
                    or str(source_record.get("status") or "").lower() != "stopped"
                ):
                    raise MigrationUnknown(
                        f"Copy source '{source_name}' changed before the final barrier."
                    )
                await _verify_copy_snapshots(
                    job,
                    source_client,
                    target_client,
                    source_name=source_name,
                    target_name=target_name,
                    expected_hash=state.get("source_snapshot_inventory_hash"),
                    label="copy_barrier",
                )

    if job.intent == "move":
        await _fenced_job_update(
            job,
            worker_id,
            phase="cutting_over",
            status="running",
            event_message="All targets passed validation; activating target workloads.",
        )

    for resource, target_name, state in transferred:
        if job.intent == "move" and state.get("original_status") == "running":
            # Persist the fence before the mutating submission. A lost response after this point
            # can never cause the source to be restarted automatically.
            state = {**state, "target_start_attempted": True}
            prepared[resource.resource_id] = state
            await _resource_update(resource.resource_id, state="cutting_over", safe_result=state)
            await _assert_mutation_lease()
            try:
                operation_id = await _blocking(
                    executor.submit_instance_state,
                    target_client,
                    project=job.target_project,
                    name=target_name,
                    action="start",
                    label=f"migration.start_target[{target_name}]",
                )
            except Exception as exc:  # noqa: BLE001
                raise MigrationUnknown(
                    f"Starting target '{target_name}' has an unknown submission outcome."
                ) from exc
            if operation_id:
                await _resource_update(
                    resource.resource_id,
                    state="cutting_over",
                    operation_ref={
                        "kind": "start_target",
                        "cluster_id": job.target_cluster_id,
                        "project": job.target_project,
                        "operation_id": operation_id,
                    },
                )
            outcome = await _blocking(
                executor.wait_operation,
                target_client,
                operation_id=operation_id,
                project=job.target_project,
                timeout=60,
                label=f"migration.wait_start_target[{target_name}]",
            )
            if not outcome.get("settled"):
                raise MigrationUnknown(
                    f"Starting target '{target_name}' needs reconciliation."
                )
            if outcome.get("failed"):
                raise RuntimeError(outcome.get("err") or f"Could not start '{target_name}'.")
            activated_target = await _blocking(
                executor.read_instance,
                target_client,
                project=job.target_project,
                name=target_name,
                label=f"migration.verify_started_target[{target_name}]",
            )
            if (
                activated_target is None
                or executor.instance_identity_fingerprint(activated_target)
                != state.get("target_identity_hash")
                or str(activated_target.get("status") or "").lower() != "running"
            ):
                raise MigrationUnknown(
                    f"Target '{target_name}' did not reach its pinned running state."
                )
            if job.strategy != "member_relocation":
                fenced_source = await _blocking(
                    executor.read_instance,
                    source_client,
                    project=job.source_project,
                    name=str((resource.source_ref or {}).get("name") or ""),
                    label=f"migration.verify_stopped_source_after_activation[{target_name}]",
                )
                if (
                    fenced_source is None
                    or executor.instance_identity_fingerprint(fenced_source)
                    != state.get("source_identity_hash")
                    or str(fenced_source.get("status") or "").lower() != "stopped"
                ):
                    raise MigrationUnknown(
                        "The retained source crossed its stopped fence during target activation."
                    )
            state = {**state, "target_started": True}
            prepared[resource.resource_id] = state
        await _resource_update(
            resource.resource_id,
            state="cutting_over" if job.intent == "move" else "ready",
            safe_result=state,
        )

    # The operation result is only evidence about the daemon operation. Re-read the entire
    # cutover set after every activation so a later start cannot hide an earlier source restart,
    # target replacement, or member drift before the job enters a retained/completed phase.
    for resource, target_name, state in transferred:
        state = prepared.get(resource.resource_id) or state
        final_target = await _blocking(
            executor.read_instance,
            target_client,
            project=job.target_project,
            name=target_name,
            label=f"migration.final_target_state[{target_name}]",
        )
        expected_status = (
            "running"
            if job.intent == "move" and state.get("original_status") == "running"
            else "stopped"
        )
        if (
            final_target is None
            or executor.instance_identity_fingerprint(final_target)
            != state.get("target_identity_hash")
            or executor.instance_fingerprint(final_target)
            != state.get("target_definition_hash")
            or str(final_target.get("status") or "").lower() != expected_status
        ):
            raise MigrationUnknown(
                f"Target '{target_name}' changed before final cutover validation."
            )
        if job.strategy == "member_relocation":
            if str(final_target.get("location") or "") != str(job.target_member or ""):
                raise MigrationUnknown(
                    f"Target '{target_name}' left the approved member before completion."
                )
            if _root_pool(final_target) != state.get("source_root_pool"):
                raise MigrationUnknown(
                    f"Target '{target_name}' changed root pool before completion."
                )
            continue
        source_name = str((resource.source_ref or {}).get("name") or "")
        if job.intent == "copy":
            final_source = await _blocking(
                executor.read_instance,
                source_client,
                project=job.source_project,
                name=source_name,
                label=f"migration.final_copy_source[{source_name}]",
            )
            if (
                final_source is None
                or executor.instance_identity_fingerprint(final_source)
                != state.get("source_identity_hash")
                or executor.instance_fingerprint(final_source)
                != state.get("source_definition_hash")
                or str(final_source.get("status") or "").lower() != "stopped"
                or not executor.instances_equivalent(final_source, final_target)
            ):
                raise MigrationUnknown(
                    f"Copy source '{source_name}' changed before completion."
                )
            await _verify_copy_snapshots(
                job,
                source_client,
                target_client,
                source_name=source_name,
                target_name=target_name,
                expected_hash=state.get("source_snapshot_inventory_hash"),
                label="final_copy",
            )
            continue
        final_source = await _blocking(
            executor.read_instance,
            source_client,
            project=job.source_project,
            name=source_name,
            label=f"migration.final_source_fence[{source_name}]",
        )
        if (
            final_source is None
            or executor.instance_identity_fingerprint(final_source)
            != state.get("source_identity_hash")
            or str(final_source.get("status") or "").lower() != "stopped"
            or not executor.instances_equivalent(final_source, final_target)
        ):
            raise MigrationUnknown(
                f"Source '{source_name}' changed before the final cutover barrier."
            )

    for resource, _target_name, state in transferred:
        state = prepared.get(resource.resource_id) or state
        await _resource_update(
            resource.resource_id,
            state=(
                "completed"
                if job.intent == "copy" or job.strategy == "member_relocation"
                else "retained"
            ),
            safe_result=state,
        )


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
    if report["blockers"]:
        raise RuntimeError("Fresh cutover preflight found blockers or inventory drift.")
    job_state = dict(job.safe_result or {})
    reason = str(job_state.get("reason") or "")
    acknowledgements = [
        str(value)
        for value in (job_state.get("acknowledgements") or [])
        if isinstance(value, str)
    ]
    validate_acknowledgements(
        intent=job.intent,
        strategy=job.strategy,
        external_cutover_required=job.external_cutover_required,
        acknowledgements=acknowledgements,
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
        post_create_report = await _fresh_worker_preflight(
            job, source_client, target_client
        )
        if post_create_report["blockers"]:
            raise MigrationUnknown(
                "Target project exists but post-create compatibility validation failed."
            )
    prepared: dict[str, dict[str, Any]] = {}
    try:
        if job.intent == "move":
            await _fenced_job_update(
                job,
                worker_id,
                phase="quiescing",
                status="running",
                event_message="Stopping source workloads for cold cutover.",
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
        await _transfer_resources(
            job,
            worker_id,
            source_client,
            target_client,
            resources,
            prepared,
            _target_pool(request),
        )
    except MigrationUnknown:
        raise
    except Exception:
        activation_attempted = any(
            state.get("target_start_attempted") for state in prepared.values()
        )
        transfer_attempted = any(
            state.get("transfer_submission_intent") for state in prepared.values()
        )
        if (
            job.intent == "move"
            and job.strategy != "member_relocation"
            and not activation_attempted
            and not transfer_attempted
        ):
            await _restart_source_best_effort(
                job, source_client, resources, prepared
            )
        raise
    completion_time = repository.utcnow().isoformat()
    completion_events = target_completion_events(
        job,
        include_created_events=job.intent == "copy",
        occurred_at=completion_time,
    )
    if job.intent == "copy" or job.strategy == "member_relocation":
        await _fenced_job_update(
            job,
            worker_id,
            phase="completed",
            status="success",
            safe_result=sanitize_payload(
                {**(job.safe_result or {}), "verified": True}
            ),
            terminal=True,
            outbox_events=completion_events,
            event_message="Migration completed and target readback was verified.",
        )
        return
    if job.external_cutover_required:
        await _fenced_job_update(
            job,
            worker_id,
            phase="external_cutover_pending",
            status="waiting",
            locked_by=None,
            lease_expires_at=None,
            outbox_events=completion_events,
            event_message="Target is ready. Confirm the external traffic cutover.",
        )
        return
    now = repository.utcnow()
    await _fenced_job_update(
        job,
        worker_id,
        phase="source_retained",
        status="waiting",
        retention_deadline=now + timedelta(hours=job.retention_hours),
        locked_by=None,
        lease_expires_at=None,
        outbox_events=completion_events,
        event_message="Cutover completed. The stopped source is retained for rollback.",
    )
