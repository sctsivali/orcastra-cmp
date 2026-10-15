"""Target preparation, source fencing, and checkpoint creation."""

from __future__ import annotations

from typing import Any

from app.core.config import get_settings
from app.models.migration_models import MigrationJob, MigrationResource

from . import executor, preflight, project_policy, repository
from .context import (
    _assert_mutation_lease,
    _fenced_job_update,
    _resource_update,
    _resources,
)
from .contracts import (
    MigrationAuthorityError,
    MigrationUnknown,
    TransferFailed,
    _blocking,
)
from .operation_wait import await_operation

async def _ensure_project_target(
    job: MigrationJob,
    worker_id: str,
    source_client: Any,
    target_client: Any,
) -> bool:
    if (
        job.source_cluster_id == job.target_cluster_id
        and job.source_project == job.target_project
    ):
        return False
    exists = await _blocking(
        executor.read_project,
        target_client,
        project=job.target_project,
        label="migration.read_target_project",
    )
    if exists is not None:
        return False
    resources = await _resources(job.job_id)
    project_resource = next(
        (resource for resource in resources if resource.resource_type == "project"), None
    )
    if project_resource is None:
        raise MigrationUnknown("The project resource journal is missing.")
    source = await _blocking(
        executor.read_project,
        source_client,
        project=job.source_project,
        label="migration.read_source_project",
    )
    if source is None:
        raise MigrationUnknown("The source project disappeared before target creation.")
    await _resource_update(
        project_resource.resource_id,
        state="preparing",
        safe_result={
            "target_project_create_intent": True,
            "target_project_create_submission_at": repository.utcnow().isoformat(),
        },
    )
    await _assert_mutation_lease()
    try:
        operation_id = await _blocking(
            executor.submit_create_project,
            source_client,
            target_client,
            source_project=job.source_project,
            target_project=job.target_project,
            job_id=job.job_id,
            label="migration.create_target_project",
        )
    except Exception as exc:  # noqa: BLE001
        if executor.is_definitive_rejection(exc):
            raise RuntimeError(
                f"The target cluster refused to create project '{job.target_project}'."
            ) from exc
        raise MigrationUnknown("Target project creation outcome is unknown.") from exc
    if operation_id:
        await _resource_update(
            project_resource.resource_id,
            state="preparing",
            operation_ref={
                "kind": "create_target_project",
                "cluster_id": job.target_cluster_id,
                "project": "default",
                "operation_id": operation_id,
            },
        )
        outcome = await await_operation(
            target_client,
            operation_id=operation_id,
            project="default",
            label="migration.wait_create_target_project",
            deadline_seconds=300,
        )
        if outcome.get("failed"):
            raise RuntimeError(outcome.get("err") or "Target project creation failed.")
    target = await _blocking(
        executor.read_project,
        target_client,
        project=job.target_project,
        label="migration.verify_target_project",
    )
    if (
        target is None
        or not executor.projects_equivalent(source, target)
        or not project_policy.created_by_job(target, job.job_id)
    ):
        raise MigrationUnknown(
            "Target project creation settled without equivalent restrictions and quotas."
        )
    await _resource_update(
        project_resource.resource_id,
        state="ready",
        safe_result={
            **(project_resource.safe_result or {}),
            "target_project_create_intent": True,
            "target_project_created": True,
            "source_project_hash": project_policy.policy_fingerprint(source),
        },
    )
    return True


async def _prepare_sources(
    job: MigrationJob,
    worker_id: str,
    source_client: Any,
    resources: list[MigrationResource],
    checkpoint_requested: bool,
) -> dict[str, dict[str, Any]]:
    prepared: dict[str, dict[str, Any]] = {}
    for resource in resources:
        if resource.resource_type != "instance":
            await _resource_update(resource.resource_id, state="ready")
            continue
        name = str((resource.source_ref or {}).get("name") or "")
        current = await _blocking(
            executor.read_instance,
            source_client,
            project=job.source_project,
            name=name,
            label=f"migration.read_source[{name}]",
        )
        if current is None:
            raise RuntimeError(f"Source instance '{name}' no longer exists.")
        pinned_identity_hash = (resource.source_ref or {}).get(
            "identity_fingerprint"
        )
        current_identity_hash = executor.instance_identity_fingerprint(current)
        if (
            not isinstance(pinned_identity_hash, str)
            or not pinned_identity_hash
            or current_identity_hash != pinned_identity_hash
        ):
            raise MigrationAuthorityError(
                f"Source instance '{name}' no longer matches its approved immutable identity.",
                code="source_identity_drift",
            )
        source_root_pool: str | None = None
        if job.strategy == "member_relocation":
            devices = current.get("expanded_devices") or current.get("devices") or {}
            roots = [
                device
                for device in devices.values()
                if isinstance(device, dict)
                and device.get("type") == "disk"
                and device.get("path") == "/"
            ] if isinstance(devices, dict) else []
            if (
                len(roots) != 1
                or not isinstance(roots[0].get("pool"), str)
                or not roots[0].get("pool")
            ):
                raise MigrationAuthorityError(
                    f"Source instance '{name}' lacks an exact effective root pool.",
                    code="source_root_pool_unverified",
                )
            source_root_pool = str(roots[0]["pool"])
        persisted = dict(resource.safe_result or {})
        reconciled_preparation = bool(
            persisted.get("stop_reconciled")
            or persisted.get("checkpoint_reconciled")
        )
        if reconciled_preparation:
            original_status = persisted.get("original_status")
            if original_status not in {"running", "stopped"}:
                raise MigrationUnknown(
                    f"Source preparation state for '{name}' lacks its original status."
                )
            if (
                str(current.get("status") or "").lower() != "stopped"
                or persisted.get("source_definition_hash")
                != executor.instance_fingerprint(current)
                or persisted.get("source_identity_hash") != pinned_identity_hash
                or (
                    job.strategy == "member_relocation"
                    and persisted.get("source_root_pool") != source_root_pool
                )
            ):
                raise MigrationUnknown(
                    f"Source '{name}' changed after preparation reconciliation."
                )
            state = persisted
            was_running = original_status == "running"
        else:
            was_running = str(current.get("status") or "").lower() == "running"
            state = {
                "original_status": "running" if was_running else "stopped",
                "checkpoint_name": None,
                "target_started": False,
                "target_start_attempted": False,
                "source_definition_hash": executor.instance_fingerprint(current),
                "source_identity_hash": pinned_identity_hash,
                "source_root_pool": source_root_pool,
            }
        prepared[resource.resource_id] = state
        await _resource_update(
            resource.resource_id, state="preparing", safe_result=state
        )
        if "transfer_bytes" not in state:
            measured = await _blocking(
                preflight._instance_transfer_bytes,
                source_client,
                job.source_project,
                name,
                label=f"migration.measure_source[{name}]",
            )
            state = {**state, "transfer_bytes": measured}
            prepared[resource.resource_id] = state
        if was_running and not state.get("stop_reconciled"):
            if job.strategy != "member_relocation" and not bool(
                getattr(get_settings(), "migration_running_source_enabled", True)
            ):
                raise TransferFailed(
                    f"Source '{name}' is running and running-source migration is disabled."
                )
            stop_timeout = int(get_settings().migration_default_stop_timeout_seconds)
            state = {
                **state,
                "stop_submission_intent": True,
                "stop_submission_at": repository.utcnow().isoformat(),
            }
            prepared[resource.resource_id] = state
            await _resource_update(
                resource.resource_id, state="preparing", safe_result=state
            )
            await _assert_mutation_lease()
            try:
                operation_id = await _blocking(
                    executor.submit_instance_state,
                    source_client,
                    project=job.source_project,
                    name=name,
                    action="stop",
                    action_timeout=stop_timeout,
                    label=f"migration.stop[{name}]",
                )
            except Exception as exc:  # noqa: BLE001
                if executor.is_definitive_rejection(exc):
                    raise TransferFailed(f"The cluster refused to stop '{name}'.") from exc
                raise MigrationUnknown(
                    f"Stop submission for '{name}' has an unknown outcome."
                ) from exc
            if operation_id:
                await _resource_update(
                    resource.resource_id,
                    state="preparing",
                    operation_ref={
                        "kind": "stop_source",
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
                    label=f"migration.wait_stop[{name}]",
                    deadline_seconds=stop_timeout + 120,
                )
            except executor.OperationForgotten:
                outcome = {"settled": True, "failed": False}
            if outcome.get("failed"):
                raise TransferFailed(
                    f"Source '{name}' did not shut down within {stop_timeout} seconds: "
                    f"{outcome.get('err') or 'stop failed'}."
                )
            stopped = await _blocking(
                executor.read_instance,
                source_client,
                project=job.source_project,
                name=name,
                label=f"migration.verify_source_stopped[{name}]",
            )
            if stopped is None or str(stopped.get("status") or "").lower() != "stopped":
                raise MigrationUnknown(
                    f"Stop operation for '{name}' settled without stopped readback."
                )

    if checkpoint_requested:
        await _fenced_job_update(
            job,
            worker_id,
            phase="checkpointing",
            status="running",
            event_message="Creating crash-consistent source checkpoints.",
        )
        for resource in resources:
            if resource.resource_type != "instance":
                continue
            name = str((resource.source_ref or {}).get("name") or "")
            checkpoint_name = f"orcastra-migrate-{job.job_id[:16]}"
            existing_state = prepared[resource.resource_id]
            if existing_state.get("checkpoint_reconciled"):
                if existing_state.get("checkpoint_name") != checkpoint_name:
                    raise MigrationUnknown(
                        f"Checkpoint identity for '{name}' changed during reconciliation."
                    )
                snapshot_hash = await _blocking(
                    executor.snapshot_inventory_fingerprint,
                    source_client,
                    project=job.source_project,
                    name=name,
                    label=f"migration.verify_reconciled_checkpoint[{name}]",
                )
                if snapshot_hash != existing_state.get(
                    "source_snapshot_inventory_hash"
                ):
                    raise MigrationUnknown(
                        f"Snapshot inventory for '{name}' changed after reconciliation."
                    )
                continue
            state = {
                **existing_state,
                "checkpoint_name": checkpoint_name,
                "checkpoint_submission_intent": True,
                "checkpoint_submission_at": repository.utcnow().isoformat(),
            }
            prepared[resource.resource_id] = state
            await _resource_update(
                resource.resource_id, state="preparing", safe_result=state
            )
            await _assert_mutation_lease()
            try:
                operation_id = await _blocking(
                    executor.submit_checkpoint,
                    source_client,
                    project=job.source_project,
                    name=name,
                    checkpoint_name=checkpoint_name,
                    label=f"migration.checkpoint[{name}]",
                )
            except Exception as exc:  # noqa: BLE001
                raise MigrationUnknown(
                    f"Checkpoint submission for '{name}' has an unknown outcome."
                ) from exc
            if operation_id:
                await _resource_update(
                    resource.resource_id,
                    state="preparing",
                    operation_ref={
                        "kind": "checkpoint",
                        "cluster_id": job.source_cluster_id,
                        "project": job.source_project,
                        "operation_id": operation_id,
                    },
                )
            outcome = await await_operation(
                source_client,
                operation_id=operation_id,
                project=job.source_project,
                label=f"migration.wait_checkpoint[{name}]",
                deadline_seconds=1800,
            )
            if outcome.get("failed"):
                raise RuntimeError(outcome.get("err") or f"Checkpoint for '{name}' failed.")

    # Pin the retained source only after every approved checkpoint exists. Cleanup later compares
    # both immutable identity evidence and the complete snapshot inventory before the first delete.
    for resource in resources:
        if resource.resource_type != "instance":
            continue
        name = str((resource.source_ref or {}).get("name") or "")
        current = await _blocking(
            executor.read_instance,
            source_client,
            project=job.source_project,
            name=name,
            label=f"migration.pin_source_identity[{name}]",
        )
        if current is None:
            raise MigrationUnknown(
                f"Source instance '{name}' disappeared before its retained identity was pinned."
            )
        if str(current.get("status") or "").lower() != "stopped":
            raise MigrationUnknown(
                f"Source instance '{name}' is not stopped at the transfer fence."
            )
        pinned_identity_hash = prepared[resource.resource_id].get(
            "source_identity_hash"
        ) or (resource.source_ref or {}).get("identity_fingerprint")
        if (
            not isinstance(pinned_identity_hash, str)
            or not pinned_identity_hash
            or executor.instance_identity_fingerprint(current) != pinned_identity_hash
        ):
            raise MigrationAuthorityError(
                f"Source instance '{name}' changed identity while the cutover fence was applied.",
                code="source_identity_drift",
            )
        snapshot_hash = await _blocking(
            executor.snapshot_inventory_fingerprint,
            source_client,
            project=job.source_project,
            name=name,
            label=f"migration.pin_source_snapshots[{name}]",
        )
        state = {
            **prepared[resource.resource_id],
            "source_definition_hash": executor.instance_fingerprint(current),
            "source_identity_hash": pinned_identity_hash,
            "source_snapshot_inventory_hash": snapshot_hash,
        }
        prepared[resource.resource_id] = state
        await _resource_update(
            resource.resource_id,
            state="preparing",
            safe_result=state,
        )
    return prepared
