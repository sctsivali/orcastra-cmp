"""Move a project's workloads to another cluster member, one instance at a time.

Each instance is stopped if it runs, moved with LXD's native cluster relocation, verified on the
new member with the same identity and root pool, and started again before the next one begins,
so at most one workload is down at any moment. Every instance keeps its identity, so there is no
retained copy: a definitive failure starts the instance in progress again where it is and moves
the ones already relocated back to their recorded member, newest first. Every step reads the
daemon first, so a restarted worker resumes without repeating a move that already happened.
"""

from __future__ import annotations

from typing import Any, Optional

from app.core.config import get_settings
from app.models.migration_models import MigrationJob, MigrationResource
from app.models.migration_schemas import MigrationPlanCreateRequest
from app.services.lxd.root_disk import root_pool

from . import executor, repository
from .completion import target_completion_events
from .context import (
    _assert_mutation_lease,
    _fenced_job_update,
    _fresh_worker_preflight,
    _job_plan,
    _resource_update,
    _resources,
)
from .contracts import MigrationAuthorityError, MigrationUnknown, TransferFailed, _blocking
from .operation_wait import await_operation, transfer_deadline_seconds
from .plan_contracts import _target_pool
from .preflight_source import active_instance_operations
from .project_preparation import watch_recorded
from .security import sanitize_error, sanitize_payload
from .transfer import _transfer_member, admit_untouched_run, untouched


def _name(resource: MigrationResource) -> str:
    return str((resource.source_ref or {}).get("name") or "")


def _status(record: Optional[dict[str, Any]]) -> str:
    return str((record or {}).get("status") or "").lower()


def _location(record: Optional[dict[str, Any]]) -> str:
    return str((record or {}).get("location") or "")


def _instances(resources: list[MigrationResource]) -> list[MigrationResource]:
    return sorted(
        (resource for resource in resources if resource.resource_type == "instance"),
        key=lambda resource: resource.ordinal,
    )


async def _read(job: MigrationJob, client: Any, name: str, label: str) -> dict[str, Any]:
    record = await _blocking(
        executor.read_instance, client, project=job.source_project, name=name, label=label
    )
    if record is None:
        raise MigrationAuthorityError(f"Instance '{name}' disappeared.", code="source_missing")
    return record


async def _set_state(
    job: MigrationJob,
    resource: MigrationResource,
    state: dict[str, Any],
    client: Any,
    *,
    action: str,
    intent_key: str,
    kind: str,
) -> None:
    """Stop or start one instance and prove it. A refused or failed operation is definitive."""
    name = _name(resource)
    await _resource_update(resource.resource_id, state="syncing", safe_result={**state, intent_key: True})
    state[intent_key] = True
    await _assert_mutation_lease()
    timeout = int(get_settings().migration_default_stop_timeout_seconds)
    try:
        operation_id = await _blocking(
            executor.submit_instance_state,
            client,
            project=job.source_project,
            name=name,
            action=action,
            **({"action_timeout": timeout} if action == "stop" else {}),
            label=f"migration.member_{kind}[{name}]",
        )
    except Exception as exc:  # noqa: BLE001
        if executor.is_definitive_rejection(exc):
            raise TransferFailed(f"The cluster refused to {action} '{name}'.") from exc
        raise MigrationUnknown(f"'{action}' of '{name}' has an unknown outcome.") from exc
    if operation_id:
        await _resource_update(
            resource.resource_id,
            state="syncing",
            operation_ref={
                "kind": kind,
                "cluster_id": job.source_cluster_id,
                "project": job.source_project,
                "operation_id": operation_id,
            },
        )
        try:
            outcome = await await_operation(
                client,
                operation_id=operation_id,
                project=job.source_project,
                label=f"migration.wait_member_{kind}[{name}]",
                deadline_seconds=timeout + 300,
            )
        except executor.OperationForgotten:
            outcome = {"settled": True, "failed": False}
        if outcome.get("failed"):
            raise TransferFailed(f"Could not {action} '{name}': {outcome.get('err') or 'failed'}.")
    wanted = "running" if action == "start" else "stopped"
    record = await _read(job, client, name, f"migration.verify_member_{kind}[{name}]")
    if _status(record) != wanted:
        raise TransferFailed(f"'{name}' did not reach {wanted} after {action}.")


async def _settle(job: MigrationJob, resource: MigrationResource, client: Any, kind: str) -> None:
    """Wait out an operation a crashed worker submitted, so readback reflects its outcome."""
    await watch_recorded(resource, kind=kind, client=client, deadline_seconds=1800)
    busy = await _blocking(
        active_instance_operations,
        client,
        {(job.source_project, _name(resource))},
        label=f"migration.member_busy[{_name(resource)}]",
    )
    if busy:
        raise MigrationUnknown(f"An operation is still running on '{_name(resource)}'.")


async def _relocate_one(
    job: MigrationJob, resource: MigrationResource, client: Any, request: MigrationPlanCreateRequest
) -> None:
    name = _name(resource)
    state = dict(resource.safe_result or {})
    if state.get("member_relocation_done"):
        return
    current = await _read(job, client, name, f"migration.member_source[{name}]")
    identity = executor.instance_identity_fingerprint(current)
    if identity != (resource.source_ref or {}).get("identity_fingerprint"):
        raise MigrationAuthorityError(
            f"Instance '{name}' no longer matches its approved identity.", code="source_identity_drift"
        )
    if "original_member" not in state:
        status = _status(current)
        if status not in {"running", "stopped"}:
            raise MigrationAuthorityError(f"Instance '{name}' is {status}.", code="source_state_changed")
        state.update(
            {
                "original_status": status,
                "original_member": _location(current),
                "source_identity_hash": identity,
                "source_root_pool": root_pool(current),
                "transfer_bytes": None,
            }
        )
        await _resource_update(resource.resource_id, state="preparing", safe_result=state)
    target_member = str(job.target_member or "")
    if state.get("stop_submission_intent") or state.get("transfer_submission_intent"):
        await _settle(job, resource, client, "transfer" if state.get("transfer_submission_intent") else "stop_source")
        current = await _read(job, client, name, f"migration.member_resume[{name}]")
    if _location(current) == state["original_member"]:
        if _status(current) == "running":
            await _set_state(job, resource, state, client, action="stop", intent_key="stop_submission_intent", kind="stop_source")
        state = await _transfer_member(job, resource, state, client, target_pool=_target_pool(request))
        current = await _read(job, client, name, f"migration.member_moved[{name}]")
    if _location(current) != target_member or root_pool(current) != state.get("source_root_pool"):
        raise MigrationUnknown(f"'{name}' is not on member '{target_member}' with its root pool.")
    if state.get("original_status") == "running" and _status(current) != "running":
        await _set_state(job, resource, state, client, action="start", intent_key="target_start_attempted", kind="start_target")
    state.update(
        {
            "target_identity_hash": identity,
            "target_definition_hash": executor.instance_fingerprint(current),
            "target_started": state.get("original_status") == "running",
            "member_relocation_done": True,
        }
    )
    await _resource_update(resource.resource_id, state="completed", safe_result=state)


async def _move_back(job: MigrationJob, resource: MigrationResource, client: Any) -> None:
    name = _name(resource)
    state = dict(resource.safe_result or {})
    original = state.get("original_member")
    if not original or state.get("member_reverted"):
        return
    await _settle(job, resource, client, "revert_relocation")
    current = await _read(job, client, name, f"migration.revert_source[{name}]")
    if executor.instance_identity_fingerprint(current) != state.get("source_identity_hash"):
        raise MigrationUnknown(f"Instance '{name}' no longer matches its identity, so it was left.")
    if _location(current) != original:
        if _status(current) != "stopped":
            await _set_state(job, resource, state, client, action="stop", intent_key="revert_stop_intent", kind="revert_stop")
        state["revert_relocation_intent"] = True
        await _resource_update(resource.resource_id, state="cleaning", safe_result=state)
        await _assert_mutation_lease()
        operation_id = await _blocking(
            executor.submit_member_relocation,
            client,
            project=job.source_project,
            name=name,
            target_member=str(original),
            label=f"migration.revert_relocation[{name}]",
        )
        if operation_id:
            await _resource_update(
                resource.resource_id,
                state="cleaning",
                operation_ref={
                    "kind": "revert_relocation",
                    "cluster_id": job.source_cluster_id,
                    "project": job.source_project,
                    "operation_id": operation_id,
                },
            )
            try:
                outcome = await await_operation(
                    client,
                    operation_id=operation_id,
                    project=job.source_project,
                    label=f"migration.wait_revert_relocation[{name}]",
                    deadline_seconds=transfer_deadline_seconds(state.get("transfer_bytes")),
                )
            except executor.OperationForgotten:
                outcome = {"settled": True, "failed": False}
            if outcome.get("failed"):
                raise MigrationUnknown(outcome.get("err") or f"Could not move '{name}' back.")
        current = await _read(job, client, name, f"migration.verify_revert[{name}]")
        if _location(current) != original:
            raise MigrationUnknown(f"'{name}' is not back on member '{original}'.")
    if state.get("original_status") == "running" and _status(current) != "running":
        try:
            await _set_state(job, resource, state, client, action="start", intent_key="revert_start_intent", kind="revert_start")
        except TransferFailed as exc:
            raise MigrationUnknown(str(exc)) from exc
    state["member_reverted"] = True
    await _resource_update(resource.resource_id, state="failed", safe_result=state)


async def _revert(job: MigrationJob, worker_id: str, client: Any) -> None:
    for resource in reversed(_instances(await _resources(job.job_id))):
        await _move_back(job, resource, client)
    state = job.safe_result or {}
    await _fenced_job_update(
        job,
        worker_id,
        phase="failed",
        status="failure",
        error_code=str(state.get("abort_code") or "member_relocation_failed"),
        error_message=str(state.get("abort_reason") or "Member relocation failed."),
        terminal=True,
        outbox_events=target_completion_events(
            job, include_created_events=False, occurred_at=repository.utcnow().isoformat()
        ),
        event_message="Relocation stopped. Every workload is back on its original member.",
    )


async def relocate_members(job: MigrationJob, worker_id: str, client: Any) -> None:
    plan = await _job_plan(job)
    request = MigrationPlanCreateRequest.model_validate(plan.request_payload)
    if job.phase == "rolling_back" or (job.safe_result or {}).get("recovery_kind") == "revert":
        await _revert(job, worker_id, client)
        return
    # important: also after a Recheck of a job that never moved anything (see project_relocation).
    if job.phase == "queued" or untouched(await _resources(job.job_id)):
        report = await _fresh_worker_preflight(job, client, client)
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
        await admit_untouched_run(job, worker_id, client, report)
    if job.phase != "final_sync":
        await _fenced_job_update(
            job, worker_id, phase="final_sync", status="running", event_message="Moving the workloads one at a time."
        )
        job.phase = "final_sync"
    try:
        for resource in _instances(await _resources(job.job_id)):
            await _relocate_one(job, resource, client, request)
    except (TransferFailed, MigrationAuthorityError) as exc:
        safe_result = sanitize_payload(
            {
                **(job.safe_result or {}),
                "recovery_kind": "revert",
                "abort_reason": sanitize_error(exc),
                "abort_code": getattr(exc, "code", "member_relocation_failed"),
            }
        )
        await _fenced_job_update(
            job,
            worker_id,
            phase="rolling_back",
            status="running",
            safe_result=safe_result,
            event_message="A workload could not be moved. Moving the others back.",
        )
        job.safe_result, job.phase = safe_result, "rolling_back"
        await _revert(job, worker_id, client)
        return
    for resource in await _resources(job.job_id):
        if resource.resource_type != "instance" and resource.state != "completed":
            await _resource_update(resource.resource_id, state="completed")
    await _fenced_job_update(
        job,
        worker_id,
        phase="completed",
        status="success",
        safe_result=sanitize_payload({**(job.safe_result or {}), "verified": True}),
        terminal=True,
        outbox_events=target_completion_events(
            job, include_created_events=False, occurred_at=repository.utcnow().isoformat()
        ),
        event_message="Every workload runs on the target member and was verified.",
    )


__all__ = ["relocate_members"]
