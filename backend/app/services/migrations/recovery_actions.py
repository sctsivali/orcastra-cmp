"""Destructive cleanup, rollback, and checkpoint recovery actions."""

from __future__ import annotations

from typing import Any

from app.models.migration_models import MigrationJob, MigrationResource

from . import executor, repository
from .context import (
    _assert_mutation_lease,
    _fenced_job_update,
    _resource_update,
    _resources,
)
from .contracts import MigrationUnknown, _blocking


# Current daemon state cannot prove that a target never accepted direct writes outside Orcastra.
# Keep destructive recovery unavailable until a durable authoritative write fence exists.
DESTRUCTIVE_RECOVERY_ENABLED = False


async def _validated_pinned_source(
    job: MigrationJob,
    resource: MigrationResource,
    source_client: Any,
    *,
    action: str,
) -> tuple[str, dict[str, Any]]:
    """Fail closed before a recovery action can trust a retained source by name."""
    state = resource.safe_result or {}
    source_name = str((resource.source_ref or {}).get("name") or "")
    source = await _blocking(
        executor.read_instance,
        source_client,
        project=job.source_project,
        name=source_name,
        label=f"migration.{action}_source_check[{source_name}]",
    )
    if source is None:
        raise RuntimeError(
            f"Retained source '{source_name}' is missing; {action} was not submitted."
        )
    if str(source.get("status") or "").lower() == "running":
        raise RuntimeError(
            f"Retained source '{source_name}' is running; {action} was not submitted."
        )
    expected_identity = state.get("source_identity_hash")
    current_identity = executor.instance_identity_fingerprint(source)
    if (
        not expected_identity
        or not current_identity
        or current_identity != expected_identity
    ):
        raise RuntimeError(
            f"Retained source '{source_name}' lacks matching immutable identity evidence."
        )
    expected_definition = state.get("source_definition_hash")
    if (
        not expected_definition
        or executor.instance_fingerprint(source) != expected_definition
    ):
        raise RuntimeError(
            f"Retained source '{source_name}' changed before {action}."
        )
    expected_snapshots = state.get("source_snapshot_inventory_hash")
    current_snapshots = await _blocking(
        executor.snapshot_inventory_fingerprint,
        source_client,
        project=job.source_project,
        name=source_name,
        label=f"migration.{action}_source_snapshots[{source_name}]",
    )
    if not expected_snapshots or current_snapshots != expected_snapshots:
        raise RuntimeError(
            f"Retained source '{source_name}' snapshot inventory changed before {action}."
        )
    return source_name, source


def _require_pinned_target(
    resource: MigrationResource,
    target_name: str,
    source: dict[str, Any],
    target: dict[str, Any],
) -> None:
    """Reject activation, replacement, or definition drift before deleting staging."""
    state = resource.safe_result or {}
    if state.get("target_started") or state.get("target_start_attempted"):
        raise RuntimeError("Reverse sync is required after target activation was attempted.")
    if str(target.get("status") or "").lower() == "running":
        raise RuntimeError(f"Target '{target_name}' is running and cannot be removed safely.")
    expected_identity = state.get("target_identity_hash")
    current_identity = executor.instance_identity_fingerprint(target)
    if (
        not expected_identity
        or not current_identity
        or current_identity != expected_identity
    ):
        raise RuntimeError(
            f"Target '{target_name}' lacks matching immutable identity evidence."
        )
    expected_definition = state.get("target_definition_hash")
    if (
        not expected_definition
        or executor.instance_fingerprint(target) != expected_definition
        or not executor.instances_equivalent(source, target)
    ):
        raise RuntimeError(f"Target '{target_name}' changed before rollback.")


def _cleanup_target_health(
    job: MigrationJob,
    target_client: Any,
    target: dict[str, Any],
    *,
    expected_running: bool,
) -> bool:
    """Require exact runtime, member, daemon trust, and root-storage evidence."""
    expected_status = "running" if expected_running else "stopped"
    if str(target.get("status") or "").lower() != expected_status:
        return False
    location = target.get("location")
    if not isinstance(location, str) or not location:
        return False
    if job.target_member and location != job.target_member:
        return False
    host_info = getattr(target_client, "host_info", None)
    if not isinstance(host_info, dict) or host_info.get("auth") != "trusted":
        return False
    devices = target.get("expanded_devices") or target.get("devices")
    if not isinstance(devices, dict):
        return False
    root_disks = [
        device
        for device in devices.values()
        if isinstance(device, dict)
        and device.get("type") == "disk"
        and device.get("path") == "/"
    ]
    return len(root_disks) == 1 and bool(root_disks[0].get("pool"))

async def _cleanup(job: MigrationJob, worker_id: str, source_client: Any, target_client: Any) -> None:
    resources = await _resources(job.job_id)
    instance_resources = [
        resource for resource in resources if resource.resource_type == "instance"
    ]
    if job.retention_deadline is None or job.retention_deadline > repository.utcnow():
        raise RuntimeError("The source retention window has not ended.")
    if job.scope == "project":
        await _fenced_job_update(
            job,
            worker_id,
            phase="cleanup_blocked",
            status="waiting",
            locked_by=None,
            lease_expires_at=None,
            error_code="non_atomic_multi_resource_cleanup",
            error_message=(
                "Automatic project cleanup is blocked until the complete namespace can be "
                "validated and deleted atomically. Review retained resources manually."
            ),
            event_message="Project cleanup was blocked to avoid partial destructive deletion.",
        )
        return

    # Validate the entire graph before the first destructive submission.
    validated: list[tuple[MigrationResource, str]] = []
    for resource in instance_resources:
        source_name = str((resource.source_ref or {}).get("name") or "")
        target_name = str((resource.target_ref or {}).get("name") or source_name)
        target = await _blocking(
            executor.read_instance,
            target_client,
            project=job.target_project,
            name=target_name,
            label=f"migration.cleanup_target_check[{target_name}]",
        )
        if target is None:
            await _fenced_job_update(
                job,
                worker_id,
                phase="cleanup_blocked",
                status="waiting",
                locked_by=None,
                lease_expires_at=None,
                error_code="target_missing",
                error_message=f"Target '{target_name}' is missing; source was not deleted.",
                event_message="Source cleanup was blocked because target validation failed.",
            )
            return
        source = await _blocking(
            executor.read_instance,
            source_client,
            project=job.source_project,
            name=source_name,
            label=f"migration.cleanup_source_check[{source_name}]",
        )
        if source is None:
            await _fenced_job_update(
                job,
                worker_id,
                phase="cleanup_blocked",
                status="waiting",
                locked_by=None,
                lease_expires_at=None,
                error_code="source_missing_before_cleanup",
                error_message="A retained source disappeared before approved cleanup.",
                event_message="Cleanup was blocked because retained source state drifted.",
            )
            return
        if str(source.get("status") or "").lower() == "running":
            await _fenced_job_update(
                job,
                worker_id,
                phase="cleanup_blocked",
                status="waiting",
                locked_by=None,
                lease_expires_at=None,
                error_code="source_not_fenced",
                error_message=f"Source '{source_name}' is running and was not deleted.",
                event_message="Cleanup was blocked because a retained source is not fenced.",
            )
            return
        expected_source_identity = (resource.safe_result or {}).get(
            "source_identity_hash"
        )
        current_source_identity = executor.instance_identity_fingerprint(source)
        if (
            not expected_source_identity
            or not current_source_identity
            or current_source_identity != expected_source_identity
        ):
            await _fenced_job_update(
                job,
                worker_id,
                phase="cleanup_blocked",
                status="waiting",
                locked_by=None,
                lease_expires_at=None,
                error_code="source_identity_drift",
                error_message=(
                    f"Source '{source_name}' lacks matching immutable identity evidence."
                ),
                event_message="Cleanup was blocked because retained source identity drifted.",
            )
            return
        expected_snapshot_hash = (resource.safe_result or {}).get(
            "source_snapshot_inventory_hash"
        )
        current_snapshot_hash = await _blocking(
            executor.snapshot_inventory_fingerprint,
            source_client,
            project=job.source_project,
            name=source_name,
            label=f"migration.cleanup_source_snapshots[{source_name}]",
        )
        if not expected_snapshot_hash or current_snapshot_hash != expected_snapshot_hash:
            await _fenced_job_update(
                job,
                worker_id,
                phase="cleanup_blocked",
                status="waiting",
                locked_by=None,
                lease_expires_at=None,
                error_code="source_snapshot_drift",
                error_message=(
                    f"Source '{source_name}' snapshot inventory changed during retention."
                ),
                event_message="Cleanup was blocked because retained snapshots drifted.",
            )
            return
        expected_hash = (resource.safe_result or {}).get("source_definition_hash")
        if not expected_hash or executor.instance_fingerprint(source) != expected_hash:
            await _fenced_job_update(
                job,
                worker_id,
                phase="cleanup_blocked",
                status="waiting",
                locked_by=None,
                lease_expires_at=None,
                error_code="source_definition_drift",
                error_message=f"Source '{source_name}' changed during retention.",
                event_message="Cleanup was blocked because retained source definition drifted.",
            )
            return
        expected_target_identity = (resource.safe_result or {}).get(
            "target_identity_hash"
        )
        current_target_identity = executor.instance_identity_fingerprint(target)
        if (
            not expected_target_identity
            or not current_target_identity
            or current_target_identity != expected_target_identity
        ):
            await _fenced_job_update(
                job,
                worker_id,
                phase="cleanup_blocked",
                status="waiting",
                locked_by=None,
                lease_expires_at=None,
                error_code="target_identity_drift",
                error_message=(
                    f"Target '{target_name}' lacks matching immutable identity evidence."
                ),
                event_message="Cleanup was blocked because target identity drifted.",
            )
            return
        if not executor.instances_equivalent(source, target):
            await _fenced_job_update(
                job,
                worker_id,
                phase="cleanup_blocked",
                status="waiting",
                locked_by=None,
                lease_expires_at=None,
                error_code="target_readback_mismatch",
                error_message=f"Target '{target_name}' no longer matches the retained source.",
                event_message="Cleanup was blocked because target readback did not match.",
            )
            return
        expected_target_definition = (resource.safe_result or {}).get(
            "target_definition_hash"
        )
        if (
            not expected_target_definition
            or executor.instance_fingerprint(target) != expected_target_definition
        ):
            await _fenced_job_update(
                job,
                worker_id,
                phase="cleanup_blocked",
                status="waiting",
                locked_by=None,
                lease_expires_at=None,
                error_code="target_definition_drift",
                error_message=f"Target '{target_name}' changed during retention.",
                event_message="Cleanup was blocked because target definition drifted.",
            )
            return
        expected_running = bool((resource.safe_result or {}).get("target_started"))
        if not _cleanup_target_health(
            job,
            target_client,
            target,
            expected_running=expected_running,
        ):
            await _fenced_job_update(
                job,
                worker_id,
                phase="cleanup_blocked",
                status="waiting",
                locked_by=None,
                lease_expires_at=None,
                error_code="target_health_unverified",
                error_message=(
                    f"Target '{target_name}' lacks exact runtime, member, daemon, or root "
                    "storage health evidence."
                ),
                event_message="Cleanup was blocked because target health changed.",
            )
            return
        target_snapshot_hash = await _blocking(
            executor.snapshot_inventory_fingerprint,
            target_client,
            project=job.target_project,
            name=target_name,
            label=f"migration.cleanup_target_snapshots[{target_name}]",
        )
        if target_snapshot_hash != current_snapshot_hash:
            await _fenced_job_update(
                job,
                worker_id,
                phase="cleanup_blocked",
                status="waiting",
                locked_by=None,
                lease_expires_at=None,
                error_code="target_snapshot_mismatch",
                error_message=(
                    f"Target '{target_name}' snapshot inventory does not match the source."
                ),
                event_message="Cleanup was blocked because snapshot inventory differs.",
            )
            return
        if resource.state not in {"retained", "completed"}:
            await _fenced_job_update(
                job,
                worker_id,
                phase="cleanup_blocked",
                status="waiting",
                locked_by=None,
                lease_expires_at=None,
                error_code="resource_not_reconciled",
                error_message=f"Resource '{source_name}' is not in a reconciled retained state.",
                event_message="Cleanup was blocked because resource reconciliation is incomplete.",
            )
            return
        validated.append((resource, source_name))

    for resource, source_name in validated:
        _source_name, current_source = await _validated_pinned_source(
            job, resource, source_client, action="cleanup_submission"
        )
        target_name = str((resource.target_ref or {}).get("name") or source_name)
        current_target = await _blocking(
            executor.read_instance,
            target_client,
            project=job.target_project,
            name=target_name,
            label=f"migration.cleanup_target_final_check[{target_name}]",
        )
        expected_target_identity = (resource.safe_result or {}).get(
            "target_identity_hash"
        )
        current_source_snapshots = await _blocking(
            executor.snapshot_inventory_fingerprint,
            source_client,
            project=job.source_project,
            name=source_name,
            label=f"migration.cleanup_source_final_snapshots[{source_name}]",
        )
        current_target_snapshots = await _blocking(
            executor.snapshot_inventory_fingerprint,
            target_client,
            project=job.target_project,
            name=target_name,
            label=f"migration.cleanup_target_final_snapshots[{target_name}]",
        )
        if (
            current_target is None
            or not expected_target_identity
            or executor.instance_identity_fingerprint(current_target)
            != expected_target_identity
            or not executor.instances_equivalent(current_source, current_target)
            or (
                str(current_target.get("status") or "").lower() == "running"
            )
            != bool((resource.safe_result or {}).get("target_started"))
            or not _cleanup_target_health(
                job,
                target_client,
                current_target,
                expected_running=bool(
                    (resource.safe_result or {}).get("target_started")
                ),
            )
            or executor.instance_fingerprint(current_target)
            != (resource.safe_result or {}).get("target_definition_hash")
            or current_source_snapshots
            != (resource.safe_result or {}).get("source_snapshot_inventory_hash")
            or current_target_snapshots != current_source_snapshots
        ):
            raise RuntimeError(
                f"Target '{target_name}' changed immediately before source cleanup."
            )
        cleanup_state = {
            **(resource.safe_result or {}),
            "cleanup_submission_intent": True,
            "cleanup_submission_at": repository.utcnow().isoformat(),
        }
        await _resource_update(
            resource.resource_id, state="cleaning", safe_result=cleanup_state
        )
        await _assert_mutation_lease()
        try:
            operation_id = await _blocking(
                executor.submit_delete_instance,
                source_client,
                project=job.source_project,
                name=source_name,
                label=f"migration.cleanup_source[{source_name}]",
            )
        except Exception as exc:  # noqa: BLE001
            raise MigrationUnknown(
                f"Cleanup submission for '{source_name}' has an unknown outcome."
            ) from exc
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
        outcome = await _blocking(
            executor.wait_operation,
            source_client,
            operation_id=operation_id,
            project=job.source_project,
            timeout=60,
            label=f"migration.wait_cleanup_source[{source_name}]",
        )
        if not outcome.get("settled"):
            raise MigrationUnknown(
                f"Cleanup of '{source_name}' needs reconciliation ({operation_id})."
            )
        if outcome.get("failed"):
            raise RuntimeError(outcome.get("err") or f"Could not delete '{source_name}'.")
        remaining = await _blocking(
            executor.read_instance,
            source_client,
            project=job.source_project,
            name=source_name,
            label=f"migration.verify_cleanup_source_absent[{source_name}]",
        )
        if remaining is not None:
            raise MigrationUnknown(
                f"Cleanup operation settled but source '{source_name}' still exists."
            )
        await _resource_update(resource.resource_id, state="completed")
    await _fenced_job_update(
        job,
        worker_id,
        phase="completed",
        status="success",
        terminal=True,
        event_message="Retained source resources were deleted after target validation.",
    )


async def _rollback(job: MigrationJob, worker_id: str, source_client: Any, target_client: Any) -> None:
    if job.strategy == "member_relocation":
        raise RuntimeError(
            "Member relocation rollback requires a separately approved reverse relocation plan."
        )
    resources = await _resources(job.job_id)
    validated: list[tuple[MigrationResource, str]] = []
    for resource in resources:
        if resource.resource_type != "instance":
            continue
        state = resource.safe_result or {}
        if state.get("target_started") or state.get("target_start_attempted"):
            raise RuntimeError(
                "Reverse sync is required after target activation was attempted."
            )
        source_name, source = await _validated_pinned_source(
            job, resource, source_client, action="rollback"
        )
        target_name = str((resource.target_ref or {}).get("name") or source_name)
        target = await _blocking(
            executor.read_instance,
            target_client,
            project=job.target_project,
            name=target_name,
            label=f"migration.rollback_target_check[{target_name}]",
        )
        if target is not None:
            _require_pinned_target(resource, target_name, source, target)
            validated.append((resource, target_name))
        else:
            await _resource_update(resource.resource_id, state="completed")

    # Validate every retained source and target before the first destructive submission.
    for resource, target_name in validated:
        state = resource.safe_result or {}
        source_name, source = await _validated_pinned_source(
            job, resource, source_client, action="rollback_submission"
        )
        target = await _blocking(
            executor.read_instance,
            target_client,
            project=job.target_project,
            name=target_name,
            label=f"migration.rollback_target_final_check[{target_name}]",
        )
        if target is None:
            await _resource_update(resource.resource_id, state="completed")
            continue
        _require_pinned_target(resource, target_name, source, target)
        rollback_state = {
            **state,
            "rollback_delete_submission_intent": True,
            "rollback_delete_submission_at": repository.utcnow().isoformat(),
        }
        await _resource_update(
            resource.resource_id, state="cleaning", safe_result=rollback_state
        )
        await _assert_mutation_lease()
        try:
            operation_id = await _blocking(
                executor.submit_delete_instance,
                target_client,
                project=job.target_project,
                name=target_name,
                label=f"migration.rollback_target[{target_name}]",
            )
        except Exception as exc:  # noqa: BLE001
            raise MigrationUnknown(
                f"Rollback submission for '{target_name}' has an unknown outcome."
            ) from exc
        if operation_id:
            await _resource_update(
                resource.resource_id,
                state="cleaning",
                operation_ref={
                    "kind": "rollback_target_delete",
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
            label=f"migration.wait_rollback_target[{target_name}]",
        )
        if not outcome.get("settled") or outcome.get("failed"):
            raise MigrationUnknown(f"Rollback deletion for '{target_name}' is unresolved.")
        remaining = await _blocking(
            executor.read_instance,
            target_client,
            project=job.target_project,
            name=target_name,
            label=f"migration.verify_rollback_target_absent[{target_name}]",
        )
        if remaining is not None:
            raise MigrationUnknown(
                f"Rollback operation settled but target '{target_name}' still exists."
            )
        await _resource_update(resource.resource_id, state="completed")
    await _fenced_job_update(
        job,
        worker_id,
        phase="rolled_back",
        status="success",
        terminal=True,
        event_message="Target staging was removed and the retained source remains authoritative.",
    )


async def _restore(job: MigrationJob, worker_id: str, source_client: Any, target_client: Any) -> None:
    if job.strategy == "member_relocation":
        raise RuntimeError(
            "Member relocation recovery requires a separately approved reverse relocation plan."
        )
    resources = await _resources(job.job_id)
    if getattr(job, "external_cutover_confirmed_at", None) is not None or any(
        (resource.safe_result or {}).get("target_started") is not False
        or (resource.safe_result or {}).get("target_start_attempted") is not False
        for resource in resources
        if resource.resource_type == "instance"
    ):
        raise RuntimeError(
            "Checkpoint restore requires reverse sync after target activation or ambiguous "
            "write history."
        )
    validated: list[tuple[MigrationResource, str, str]] = []
    for resource in resources:
        if resource.resource_type != "instance":
            continue
        state = resource.safe_result or {}
        checkpoint_name = state.get("checkpoint_name")
        if not checkpoint_name:
            raise RuntimeError("No checkpoint is recorded for this workload.")
        source_name, _source = await _validated_pinned_source(
            job, resource, source_client, action="restore"
        )
        target_name = str((resource.target_ref or {}).get("name") or source_name)
        target = await _blocking(
            executor.read_instance,
            target_client,
            project=job.target_project,
            name=target_name,
            label=f"migration.restore_target_check[{target_name}]",
        )
        if target is not None:
            raise RuntimeError("Remove or reconcile the target before restoring the source.")
        validated.append((resource, source_name, str(checkpoint_name)))

    # Validate the full recovery set before the first destructive restore submission.
    for resource, source_name, checkpoint_name in validated:
        state = resource.safe_result or {}
        await _validated_pinned_source(
            job, resource, source_client, action="restore_submission"
        )
        target_name = str((resource.target_ref or {}).get("name") or source_name)
        target = await _blocking(
            executor.read_instance,
            target_client,
            project=job.target_project,
            name=target_name,
            label=f"migration.restore_target_final_check[{target_name}]",
        )
        if target is not None:
            raise RuntimeError("A target appeared before checkpoint restoration.")
        restore_state = {
            **state,
            "restore_submission_intent": True,
            "restore_submission_at": repository.utcnow().isoformat(),
        }
        await _resource_update(
            resource.resource_id, state="preparing", safe_result=restore_state
        )
        await _assert_mutation_lease()
        try:
            operation_id = await _blocking(
                executor.submit_restore_checkpoint,
                source_client,
                project=job.source_project,
                name=source_name,
                checkpoint_name=checkpoint_name,
                label=f"migration.restore_checkpoint[{source_name}]",
            )
        except Exception as exc:  # noqa: BLE001
            raise MigrationUnknown(
                f"Checkpoint restore submission for '{source_name}' is unknown."
            ) from exc
        if operation_id:
            await _resource_update(
                resource.resource_id,
                state="preparing",
                operation_ref={
                    "kind": "restore_checkpoint",
                    "cluster_id": job.source_cluster_id,
                    "project": job.source_project,
                    "operation_id": operation_id,
                },
            )
        outcome = await _blocking(
            executor.wait_operation,
            source_client,
            operation_id=operation_id,
            project=job.source_project,
            timeout=60,
            label=f"migration.wait_restore_checkpoint[{source_name}]",
        )
        if not outcome.get("settled") or outcome.get("failed"):
            raise MigrationUnknown(f"Checkpoint restore for '{source_name}' is unresolved.")
        restored = await _blocking(
            executor.read_instance,
            source_client,
            project=job.source_project,
            name=source_name,
            label=f"migration.verify_restored_source[{source_name}]",
        )
        expected_identity = state.get("source_identity_hash")
        expected_definition = state.get("source_definition_hash")
        if (
            restored is None
            or not expected_identity
            or executor.instance_identity_fingerprint(restored) != expected_identity
            or not expected_definition
            or executor.instance_fingerprint(restored) != expected_definition
            or str(restored.get("status") or "").lower() == "running"
        ):
            raise MigrationUnknown(
                f"Checkpoint restore for '{source_name}' settled without exact readback."
            )
        if state.get("original_status") == "running":
            restore_state = {
                **restore_state,
                "restore_start_submission_intent": True,
                "restore_start_submission_at": repository.utcnow().isoformat(),
            }
            await _resource_update(
                resource.resource_id, state="preparing", safe_result=restore_state
            )
            await _assert_mutation_lease()
            try:
                operation_id = await _blocking(
                    executor.submit_instance_state,
                    source_client,
                    project=job.source_project,
                    name=source_name,
                    action="start",
                    label=f"migration.restore_start[{source_name}]",
                )
            except Exception as exc:  # noqa: BLE001
                raise MigrationUnknown(
                    f"Restored source start submission for '{source_name}' is unknown."
                ) from exc
            if operation_id:
                await _resource_update(
                    resource.resource_id,
                    state="preparing",
                    operation_ref={
                        "kind": "restore_source_start",
                        "cluster_id": job.source_cluster_id,
                        "project": job.source_project,
                        "operation_id": operation_id,
                    },
                )
            outcome = await _blocking(
                executor.wait_operation,
                source_client,
                operation_id=operation_id,
                project=job.source_project,
                timeout=60,
                label=f"migration.wait_restore_start[{source_name}]",
            )
            if not outcome.get("settled") or outcome.get("failed"):
                raise MigrationUnknown(f"Restart after restoring '{source_name}' is unresolved.")
            restored = await _blocking(
                executor.read_instance,
                source_client,
                project=job.source_project,
                name=source_name,
                label=f"migration.verify_restored_start[{source_name}]",
            )
            if (
                restored is None
                or executor.instance_identity_fingerprint(restored) != expected_identity
                or str(restored.get("status") or "").lower() != "running"
            ):
                raise MigrationUnknown(
                    f"Restart for restored source '{source_name}' lacks exact readback."
                )
        await _resource_update(resource.resource_id, state="completed")
    await _fenced_job_update(
        job,
        worker_id,
        phase="rolled_back",
        status="success",
        terminal=True,
        event_message="Source checkpoints were restored and verified.",
    )
