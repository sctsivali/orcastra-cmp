"""Durable migration worker dispatch, recovery, heartbeat, and audit delivery."""

from __future__ import annotations

import asyncio
import secrets
import socket
from typing import Any

from app.core.database import async_session_maker
from app.models.migration_models import MigrationJob, MigrationResource
from app.services.activity_logger import get_activity_logger

from . import repository
from .completion import deliver_completion_event
from .context import (
    _authoritative_clients,
    _fenced_job_update,
    _fresh_worker_preflight,
    _job_plan,
    _resources,
    _wait_for_cutover,
)
from .contracts import (
    MigrationAuthorityError,
    MigrationLeaseLost,
    MigrationUnknown,
    _HEARTBEAT_SECONDS,
    _worker_fence,
    logger,
)
from .reconciliation import _reconcile, _reconciliation_backoff_values
from .recovery_actions import (
    DESTRUCTIVE_RECOVERY_ENABLED,
    _cleanup,
    _restore,
    _rollback,
)
from .security import MigrationSecurityError, sanitize_error, sanitize_payload
from .transfer import _perform_copy_or_cutover

activity_logger = get_activity_logger()
_tasks: set[asyncio.Task[Any]] = set()

def _schedule(coro: Any) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _heartbeat_loop(job_id: str, worker_id: str, fence_token: int) -> None:
    while True:
        await asyncio.sleep(_HEARTBEAT_SECONDS)
        try:
            async with async_session_maker() as db:
                renewed = await repository.heartbeat(db, job_id, worker_id, fence_token)
            if not renewed:
                return
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not renew migration lease %s: %s", job_id, sanitize_error(exc)
            )


def _safe_mutation_resume(
    job: MigrationJob, resources: list[MigrationResource]
) -> bool:
    """Whether a persisted approval is known to precede its next daemon submission."""
    if job.status in {"unknown", "manual_recovery"}:
        return False
    instance_resources = [
        resource for resource in resources if resource.resource_type == "instance"
    ]
    if job.phase == "queued" or (
        job.phase == "initial_sync" and job.intent == "copy"
    ):
        mutation_markers = {
            "stop_submission_intent",
            "checkpoint_submission_intent",
            "transfer_submission_intent",
            "source_submission_intent",
            "target_submission_intent",
            "target_start_attempted",
        }
        return all(
            not resource.operation_refs
            and not any((resource.safe_result or {}).get(key) for key in mutation_markers)
            and resource.state in {"pending", "blocked", "ready"}
            for resource in instance_resources
        )
    if job.phase == "preparing_target":
        project_resources = [
            resource
            for resource in resources
            if resource.resource_type == "project"
            and (resource.safe_result or {}).get("target_project_create_intent")
        ]
        return len(project_resources) == 1 and bool(
            (project_resources[0].safe_result or {}).get(
                "target_project_reconciled"
            )
        ) and all(
            not (resource.safe_result or {}).get("transfer_submission_intent")
            for resource in instance_resources
        )
    if job.phase == "quiescing":
        return all(
            not any(
                (resource.safe_result or {}).get(key)
                for key in (
                    "checkpoint_submission_intent",
                    "transfer_submission_intent",
                    "source_submission_intent",
                    "target_submission_intent",
                )
            )
            and (
                not (resource.safe_result or {}).get("stop_submission_intent")
                or bool((resource.safe_result or {}).get("stop_reconciled"))
            )
            for resource in instance_resources
        )
    if job.phase == "checkpointing":
        return bool(instance_resources) and all(
            (resource.safe_result or {}).get("checkpoint_submission_intent")
            and (resource.safe_result or {}).get("checkpoint_reconciled")
            and not any(
                (resource.safe_result or {}).get(key)
                for key in (
                    "transfer_submission_intent",
                    "source_submission_intent",
                    "target_submission_intent",
                )
            )
            for resource in instance_resources
        )
    action_contract = {
        "cleaning_up": ("cleanup_source", "cleanup_submission_intent"),
        "rolling_back": (
            "rollback_target_delete",
            "rollback_delete_submission_intent",
        ),
        "preparing_recovery": (
            "restore_checkpoint",
            "restore_submission_intent",
        ),
    }
    expected = action_contract.get(job.phase)
    if expected is None:
        return False
    operation_kind, intent_key = expected
    return all(
        not (resource.safe_result or {}).get(intent_key)
        and all(
            operation_ref.get("kind") != operation_kind
            for operation_ref in resource.operation_refs or []
        )
        for resource in instance_resources
    )


async def _run_claimed(
    job: MigrationJob, worker_id: str, *, allow_mutation: bool
) -> None:
    # Validate the immutable plan pin before any daemon read or mutation. A job must never
    # inherit a later plan revision or a changed payload through the foreign-key relationship.
    plan = await _job_plan(job)
    checkpoint_requested = bool(getattr(plan, "checkpoint_requested", False))

    async def stop_at_contract_gate(
        *, code: str, message: str, remediation: str, event_message: str
    ) -> None:
        resources = await _resources(job.job_id)
        mutation_markers = {
            "target_project_create_intent",
            "stop_submission_intent",
            "checkpoint_submission_intent",
            "transfer_submission_intent",
            "source_submission_intent",
            "target_submission_intent",
            "target_start_attempted",
            "restore_start_submission_intent",
        }
        safely_pre_mutation = getattr(job, "phase", None) == "queued" and all(
            not resource.operation_refs
            and not any(
                (resource.safe_result or {}).get(key) for key in mutation_markers
            )
            for resource in resources
        )
        transition: dict[str, Any] = {
            "phase": "failed" if safely_pre_mutation else "manual_recovery",
            "status": "failure" if safely_pre_mutation else "manual_recovery",
            "error_code": (
                code if safely_pre_mutation else f"{code}_mutation_state_ambiguous"
            ),
            "error_message": (
                message
                if safely_pre_mutation
                else f"{message} Persisted mutation evidence requires manual recovery."
            ),
            "safe_result": sanitize_payload(
                {
                    **(job.safe_result or {}),
                    "blockers": [
                        {
                            "code": code,
                            "message": message,
                            "remediation": remediation,
                        }
                    ],
                }
            ),
        }
        if not safely_pre_mutation:
            transition.update({"locked_by": None, "lease_expires_at": None})
        await _fenced_job_update(
            job,
            worker_id,
            **transition,
            terminal=safely_pre_mutation,
            event_message=(
                event_message
                if safely_pre_mutation
                else "Invalid contract found after possible mutation; resource locks remain held."
            ),
        )

    if getattr(job, "intent", None) == "copy" and checkpoint_requested:
        message = (
            "Copy cannot request an automatic checkpoint because the copy executor does "
            "not create or reconcile that checkpoint."
        )
        await stop_at_contract_gate(
            code="copy_checkpoint_unsupported",
            message=message,
            remediation="Disable the checkpoint option and create a new plan.",
            event_message="Execution stopped at the copy checkpoint contract gate.",
        )
        return
    if getattr(job, "strategy", None) == "member_relocation" and (
        checkpoint_requested or getattr(job, "scope", None) == "project"
    ):
        code = (
            "member_relocation_checkpoint_unrecoverable"
            if checkpoint_requested
            else "project_member_relocation_recovery_unavailable"
        )
        message = (
            "Member relocation cannot create a checkpoint because this release has no "
            "reachable restore or cleanup path for that snapshot."
            if checkpoint_requested
            else "Project member relocation is blocked until partial relocation can be "
            "reversed inside the original locked job."
        )
        await stop_at_contract_gate(
            code=code,
            message=message,
            remediation=(
                "Disable checkpoints for single-instance member relocation."
                if checkpoint_requested
                else "Move one instance at a time after verifying target capacity."
            ),
            event_message="Execution stopped at the member relocation recovery gate.",
        )
        return
    source_client, target_client = await _authoritative_clients(job)
    if not allow_mutation:
        await _reconcile(job, worker_id, source_client, target_client)
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
                safe_result=sanitize_payload(
                    {**(job.safe_result or {}), "blockers": report["blockers"]}
                ),
                terminal=True,
                event_message="Execution stopped because fresh preflight found blockers.",
            )
            return
        if job.intent == "move":
            await _wait_for_cutover(job, worker_id)
            return
        await _perform_copy_or_cutover(
            job, worker_id, source_client, target_client
        )
        return
    if job.phase in {"quiescing", "preparing_target", "checkpointing"}:
        await _perform_copy_or_cutover(job, worker_id, source_client, target_client)
    elif job.phase == "initial_sync" and job.intent == "copy":
        await _perform_copy_or_cutover(
            job, worker_id, source_client, target_client
        )
    elif job.phase in {
        "initial_sync",
        "final_sync",
        "cutting_over",
    }:
        # A worker reached these phases only after crossing a durable mutation boundary. A
        # restarted worker observes daemon state; it never repeats the prior submission.
        await _reconcile(job, worker_id, source_client, target_client)
    elif job.phase in {"unknown", "reconciling", "manual_recovery"} or job.status == "unknown":
        await _reconcile(job, worker_id, source_client, target_client)
    elif job.phase in {"cleaning_up", "rolling_back", "preparing_recovery"}:
        if not DESTRUCTIVE_RECOVERY_ENABLED:
            await _fenced_job_update(
                job,
                worker_id,
                phase="manual_recovery",
                status="manual_recovery",
                locked_by=None,
                lease_expires_at=None,
                error_code="destructive_recovery_write_history_unverified",
                error_message=(
                    "Destructive recovery is disabled until authoritative target write "
                    "history can be proven."
                ),
                event_message="Destructive recovery was stopped at the current release gate.",
            )
            return
        if job.phase == "cleaning_up":
            await _cleanup(job, worker_id, source_client, target_client)
        elif job.phase == "rolling_back":
            await _rollback(job, worker_id, source_client, target_client)
        else:
            await _restore(job, worker_id, source_client, target_client)


async def run_migration_job(job_id: str, *, allow_mutation: bool = False) -> None:
    worker_id = f"{socket.gethostname()}:{secrets.token_hex(8)}"
    async with async_session_maker() as db:
        job = await repository.claim_job(db, job_id, worker_id)
    if job is None:
        return
    heartbeat = asyncio.create_task(
        _heartbeat_loop(job.job_id, worker_id, job.fence_token)
    )
    fence_token = _worker_fence.set((job.job_id, worker_id, job.fence_token))
    try:
        await _run_claimed(job, worker_id, allow_mutation=allow_mutation)
    except MigrationLeaseLost:
        logger.warning("Migration worker %s lost its lease; no further mutation will run", job_id)
    except MigrationAuthorityError as exc:
        await _fenced_job_update(
            job,
            worker_id,
            phase="manual_recovery",
            status="manual_recovery",
            locked_by=None,
            lease_expires_at=None,
            error_code=exc.code,
            error_message=sanitize_error(exc),
            event_message="Migration paused because authorization or cluster identity changed.",
        )
    except (MigrationUnknown, MigrationSecurityError) as exc:
        values = _reconciliation_backoff_values(
            job,
            error_code="operation_outcome_unknown",
            error_message=sanitize_error(exc),
        )
        await _fenced_job_update(
            job,
            worker_id,
            **values,
        )
    except Exception as exc:  # noqa: BLE001
        # Only explicit fresh-preflight blockers above are known to precede mutation. Once a
        # worker enters execution, a lost response may hide an accepted daemon operation, so a
        # generic exception is never allowed to release resource locks or invite a retry.
        await _fenced_job_update(
            job,
            worker_id,
            phase="manual_recovery",
            status="manual_recovery",
            locked_by=None,
            lease_expires_at=None,
            error_code="migration_failed",
            error_message=sanitize_error(exc),
            terminal=False,
            event_message="Migration needs manual recovery before any submission can be retried.",
        )
        logger.error(
            "Migration job %s entered manual recovery: %s",
            job_id,
            sanitize_error(exc),
        )
    finally:
        _worker_fence.reset(fence_token)
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)


async def recover_migration_jobs() -> int:
    """Run one idempotent recovery sweep; database leases arbitrate multiple workers."""
    async with async_session_maker() as db:
        await repository.mark_expired_plans(db)
        await repository.cleanup_due_jobs(db)
        job_ids = await repository.recoverable_job_ids(db)
        recovery: list[tuple[str, bool]] = []
        for job_id in job_ids:
            job = await repository.get_job(db, job_id)
            if job is None:
                continue
            resources = await repository.list_resources(db, job_id)
            recovery.append((job_id, _safe_mutation_resume(job, resources)))
    for job_id, allow_mutation in recovery:
        _schedule(run_migration_job(job_id, allow_mutation=allow_mutation))
    return len(job_ids)


async def drain_migration_audits() -> int:
    """Deliver one bounded outbox batch to the structured audit sink."""
    worker_id = f"{socket.gethostname()}:{secrets.token_hex(8)}"
    async with async_session_maker() as db:
        rows = await repository.pending_outbox_rows(db, worker_id=worker_id)
        jobs = {
            job_id: await repository.get_job(db, job_id)
            for job_id in {row.job_id for row in rows}
        }
    delivered = 0
    for row in rows:
        payload = row.payload or {}
        job = jobs.get(row.job_id)
        try:
            if row.channel == "audit":
                actor = payload.get("actor")
                if not isinstance(actor, dict):
                    raise RuntimeError("Migration audit event has no immutable actor snapshot.")
                actor_email = actor.get("email")
                if not isinstance(actor_email, str) or not actor_email:
                    raise RuntimeError("Migration audit event actor snapshot is incomplete.")
                activity_logger.log_activity(
                    action=row.event_type,
                    target_type="migration",
                    target_id=row.job_id,
                    result="success",
                    details=sanitize_payload(payload),
                    event_id=row.event_id,
                    actor_user_id=actor_email,
                    target_host=job.target_cluster_id if job is not None else None,
                    target_project=job.target_project if job is not None else None,
                    target_organization_id=(
                        job.target_organization_id if job is not None else None
                    ),
                    target_organization_resolution="exact" if job is not None else None,
                )
            elif not await deliver_completion_event(row):
                raise RuntimeError(
                    f"Unsupported migration outbox channel '{row.channel}'."
                )
            async with async_session_maker() as db:
                await repository.finish_outbox(
                    db, row.event_id, worker_id=worker_id, delivered=True
                )
            delivered += 1
        except Exception as exc:  # noqa: BLE001
            async with async_session_maker() as db:
                await repository.finish_outbox(
                    db,
                    row.event_id,
                    worker_id=worker_id,
                    delivered=False,
                    error=sanitize_error(exc),
                )
    return delivered
