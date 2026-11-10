"""Prepare a project relocation's target and its sources, resumably.

The target project is created with this job's marker, and every profile is cloned exactly as the
plan approved, re-derived from the live source profiles with the planner's view of the target.
A stop interrupted by a crash is settled by readback before the sources are prepared again.
"""

from __future__ import annotations

from typing import Any

from app.models.migration_models import MigrationJob, MigrationResource
from app.models.migration_schemas import MigrationPlanCreateRequest

from . import executor, executor_project, project_policy, repository
from .context import _assert_mutation_lease, _resource_update, _resources
from .contracts import MigrationAuthorityError, MigrationUnknown, TransferFailed, _blocking
from .operation_wait import await_operation
from .profile_clone import clone_profiles, profile_fingerprint
from .target_admission import read_target_view


def _names(resource: MigrationResource) -> tuple[str, str]:
    source_name = str((resource.source_ref or {}).get("name") or "")
    return source_name, str((resource.target_ref or {}).get("name") or source_name)


def _of_type(resources: list[MigrationResource], kind: str) -> list[MigrationResource]:
    return sorted(
        (resource for resource in resources if resource.resource_type == kind),
        key=lambda resource: resource.ordinal,
    )


def _status(record: Any) -> str:
    return str((record or {}).get("status") or "").lower()


async def watch_recorded(
    resource: MigrationResource, *, kind: str, client: Any, deadline_seconds: int
) -> str:
    """Outcome of the last recorded operation of ``kind``: settled, failed, gone, or absent."""
    refs = [
        ref
        for ref in (resource.operation_refs or [])
        if ref.get("kind") == kind and ref.get("operation_id")
    ]
    if not refs:
        return "absent"
    ref = refs[-1]
    try:
        outcome = await await_operation(
            client,
            operation_id=str(ref["operation_id"]),
            project=str(ref.get("project") or "default"),
            label=f"migration.project_watch[{kind}]",
            deadline_seconds=deadline_seconds,
        )
    except executor.OperationForgotten:
        return "gone"
    return "failed" if outcome.get("failed") else "settled"


async def ensure_target_project(
    job: MigrationJob, source_client: Any, target_client: Any, resource: MigrationResource
) -> None:
    state = dict(resource.safe_result or {})
    source = await _blocking(
        executor.read_project, source_client, project=job.source_project, label="migration.source_project"
    )
    if source is None:
        raise MigrationAuthorityError("The source project disappeared.", code="source_project_missing")
    target = await _blocking(
        executor.read_project, target_client, project=job.target_project, label="migration.target_project"
    )
    if target is None:
        # LXD creates a project synchronously, so an absent project after a recorded intent
        # means the earlier create never happened. Submitting again is safe.
        state.update(
            {
                "target_project_create_intent": True,
                "target_project_create_submission_at": repository.utcnow().isoformat(),
            }
        )
        await _resource_update(resource.resource_id, state="preparing", safe_result=state)
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
                raise TransferFailed(
                    f"The target cluster refused to create project '{job.target_project}'."
                ) from exc
            raise MigrationUnknown("Target project creation has an unknown outcome.") from exc
        if operation_id:
            await await_operation(
                target_client,
                operation_id=operation_id,
                project="default",
                label="migration.wait_create_target_project",
                deadline_seconds=300,
            )
        target = await _blocking(
            executor.read_project, target_client, project=job.target_project, label="migration.verify_target_project"
        )
    if target is None or not project_policy.created_by_job(target, job.job_id):
        raise MigrationAuthorityError(
            f"Project '{job.target_project}' on the target was not created by this migration.",
            code="target_project_not_owned",
        )
    if not executor.projects_equivalent(source, target):
        raise MigrationUnknown("The new target project does not enforce the source project policy.")
    state.update(
        {
            "target_project_create_intent": True,
            "target_project_created": True,
            "target_project_reconciled": True,
            "source_project_hash": project_policy.policy_fingerprint(source),
        }
    )
    await _resource_update(resource.resource_id, state="ready", safe_result=state)


async def clone_target_profiles(
    job: MigrationJob,
    source_client: Any,
    target_client: Any,
    request: MigrationPlanCreateRequest,
    resources: list[MigrationResource],
) -> None:
    profiles = _of_type(resources, "profile")
    if not profiles:
        return
    target_project = await _blocking(
        executor.read_project, target_client, project=job.target_project, label="migration.clone_target_project"
    )
    # important: a profile write to a project without its own profiles lands in the default
    # project's, which every project on the cluster may share.
    if not project_policy.features_enabled(target_project, "profiles") or not project_policy.created_by_job(
        target_project, job.job_id
    ):
        raise MigrationUnknown("The new target project does not isolate its profiles.")
    source_project = await _blocking(
        executor.read_project, source_client, project=job.source_project, label="migration.clone_source_project"
    )
    rows = await _blocking(
        executor_project.list_profiles, source_client, project=job.source_project, label="migration.source_profiles"
    )
    # The same simulated view the planner used, so the clone is derived identically.
    view = await _blocking(
        read_target_view,
        target_client=target_client,
        target_project=job.target_project,
        target_project_record=None,
        source_project_record=source_project,
        profile_names=[],
        label="migration.clone_target_view",
    )
    clones = {
        clone.name: clone
        for clone in clone_profiles(
            rows,
            mappings=request.mappings.model_dump(mode="json"),
            usable_networks=view.usable_networks,
            pools=view.pools,
        )
    }
    if set(clones) != {_names(resource)[0] for resource in profiles}:
        raise MigrationAuthorityError(
            "The source project's profiles changed after approval.", code="source_profiles_changed"
        )
    for resource in profiles:
        name, _target = _names(resource)
        clone = clones[name]
        approved = (resource.target_ref or {}).get("definition_fingerprint")
        if clone.findings or clone.fingerprint != approved:
            raise MigrationAuthorityError(
                f"Profile '{name}' changed after approval.", code="profile_contract_drift"
            )
        current = await _blocking(
            executor_project.read_profile,
            target_client,
            project=job.target_project,
            name=name,
            label=f"migration.target_profile[{name}]",
        )
        state = dict(resource.safe_result or {})
        if current is None or profile_fingerprint(current) != approved:
            if current is not None and name != "default":
                raise MigrationUnknown(
                    f"Profile '{name}' in the new project has a definition this job did not write."
                )
            state.update({"profile_clone_intent": True})
            await _resource_update(resource.resource_id, state="preparing", safe_result=state)
            await _assert_mutation_lease()
            write = executor_project.replace_profile if current is not None else executor_project.create_profile
            try:
                await _blocking(
                    write,
                    target_client,
                    project=job.target_project,
                    record=clone.record(),
                    label=f"migration.clone_profile[{name}]",
                )
            except Exception as exc:  # noqa: BLE001
                if executor.is_definitive_rejection(exc):
                    raise TransferFailed(f"The target refused profile '{name}'.") from exc
                raise MigrationUnknown(f"Writing profile '{name}' has an unknown outcome.") from exc
            current = await _blocking(
                executor_project.read_profile,
                target_client,
                project=job.target_project,
                name=name,
                label=f"migration.verify_profile[{name}]",
            )
            if profile_fingerprint(current) != approved:
                raise MigrationUnknown(f"Profile '{name}' did not read back as approved.")
        state.update({"profile_clone_intent": True, "profile_cloned": True})
        await _resource_update(resource.resource_id, state="ready", safe_result=state)


async def settle_interrupted_stops(job: MigrationJob, source_client: Any) -> None:
    """A stop submitted before a crash either took effect or it did not: decide which."""
    for resource in _of_type(await _resources(job.job_id), "instance"):
        state = dict(resource.safe_result or {})
        if (
            not state.get("stop_submission_intent")
            or state.get("stop_reconciled")
            # A pinned source was proven stopped when it was pinned.
            or state.get("source_snapshot_inventory_hash")
        ):
            continue
        name, _target = _names(resource)
        await watch_recorded(resource, kind="stop_source", client=source_client, deadline_seconds=900)
        current = await _blocking(
            executor.read_instance, source_client, project=job.source_project, name=name, label=f"migration.settle_stop[{name}]"
        )
        if current is None or executor.instance_identity_fingerprint(current) != state.get(
            "source_identity_hash"
        ):
            raise MigrationAuthorityError(
                f"Source '{name}' changed while it was being stopped.", code="source_identity_drift"
            )
        if _status(current) == "running":
            raise TransferFailed(f"Source '{name}' did not shut down, so the relocation stopped.")
        if _status(current) != "stopped":
            raise MigrationUnknown(f"Source '{name}' is {_status(current) or 'unreadable'}.")
        state["stop_reconciled"] = True
        await _resource_update(resource.resource_id, state="preparing", safe_result=state)


__all__ = [
    "clone_target_profiles",
    "ensure_target_project",
    "settle_interrupted_stops",
    "watch_recorded",
]
