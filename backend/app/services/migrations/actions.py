"""Authorized operator transitions for durable migration jobs."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

from app.core.org_scope import get_user_org_ids
from app.models.migration_models import MigrationJob

from . import executor, repository
from .authority import AuthentikAuthorityVerifier
from .context import _authoritative_clients, _job_plan
from .contracts import (
    MigrationAuthorityError,
    MigrationConflict,
    MigrationNotFound,
    _authority_values,
    _blocking,
)
from .security import sanitize_payload
from .recovery_actions import DESTRUCTIVE_RECOVERY_ENABLED
from .worker import _schedule, run_migration_job


def _reset_reconciliation_cycle(value: Any) -> dict[str, Any]:
    state = dict(value) if isinstance(value, dict) else {}
    raw_cycle = state.get("reconciliation_cycle", 0)
    cycle = raw_cycle if isinstance(raw_cycle, int) and not isinstance(raw_cycle, bool) else 0
    state["reconciliation_cycle"] = max(0, cycle) + 1
    state["reconciliation_streak"] = 0
    state.pop("reconciliation_retry_delay_seconds", None)
    return sanitize_payload(state)


class MigrationActionsMixin:
    async def act(
        self,
        job_id: str,
        *,
        action: str,
        expected_revision: int,
        reason: str,
        confirmation: Optional[str] = None,
        user: Any,
        requester_role: str,
    ) -> MigrationJob:
        row = await repository.get_job(self.db, job_id, lock=True)
        if row is None:
            raise MigrationNotFound("Migration job not found.")
        if row.revision != expected_revision:
            raise MigrationConflict("The migration changed. Reload it before acting.")
        await _job_plan(row)
        authority = await AuthentikAuthorityVerifier(self.settings).pin_user(user)
        requester_role = authority.role.value
        if requester_role == "partner":
            organization_ids = set(await get_user_org_ids(user, self.db))
            if row.source_organization_id not in organization_ids:
                raise MigrationAuthorityError(
                    "The current operator is not affiliated with this migration organization."
                )
        # Request ContextVars do not authorize a durable action. Rebuild ownership,
        # organization affiliation, registration fingerprints, and daemon identity now.
        _source_client, target_client = await _authoritative_clients(
            row, current_authority=authority
        )
        now = repository.utcnow()
        values: dict[str, Any]
        terminal = False
        if action == "cancel":
            if row.phase not in {"queued", "cutover_pending"}:
                raise MigrationConflict("This phase can no longer be cancelled safely.")
            if row.status == "running" or (
                row.lease_expires_at is not None and row.lease_expires_at > now
            ):
                raise MigrationConflict(
                    "Cancellation cannot race an active worker; retry after its lease ends."
                )
            resources = await repository.list_resources(self.db, row.job_id)
            if any(
                resource.operation_refs or resource.state not in {"pending", "blocked"}
                for resource in resources
            ):
                raise MigrationConflict(
                    "Cancellation is blocked because a daemon mutation may already exist."
                )
            values = {
                "phase": "cancelled",
                "status": "cancelled",
                "cancel_requested_at": now,
                "safe_result": sanitize_payload(
                    {**(row.safe_result or {}), "cancel_reason": reason}
                ),
            }
            terminal = True
        elif action == "cutover":
            if row.phase != "cutover_pending" or row.status != "waiting":
                raise MigrationConflict("The migration is not waiting for cutover.")
            values = {
                "phase": "quiescing",
                "status": "pending",
                "cutover_approved_at": now,
                "locked_by": None,
                "lease_expires_at": None,
            }
        elif action == "confirm_external_cutover":
            if row.phase != "external_cutover_pending" or row.status != "waiting":
                raise MigrationConflict("External cutover confirmation is not pending.")
            values = {
                "phase": "source_retained",
                "status": "waiting",
                "external_cutover_confirmed_at": now,
                "retention_deadline": now + timedelta(hours=row.retention_hours),
            }
        elif action == "rollback":
            if not DESTRUCTIVE_RECOVERY_ENABLED:
                raise MigrationConflict(
                    "Rollback is disabled until authoritative target write history can be proven."
                )
            if confirmation != "ROLLBACK":
                raise MigrationConflict("Type ROLLBACK to confirm reverse cutover.")
            if row.phase not in {"source_retained", "cleanup_due", "rollback_available"}:
                raise MigrationConflict("Rollback is not available in this phase.")
            if row.strategy == "member_relocation":
                raise MigrationConflict(
                    "Member relocation rollback requires a new reverse relocation plan."
                )
            resources = await repository.list_resources(self.db, row.job_id)
            if any(
                (resource.safe_result or {}).get("target_started")
                or (resource.safe_result or {}).get("target_start_attempted")
                for resource in resources
            ):
                raise MigrationConflict(
                    "Automated rollback is blocked after target writes; reverse sync is required."
                )
            for resource in resources:
                if resource.resource_type != "instance":
                    continue
                target_name = str(
                    (resource.target_ref or {}).get("name")
                    or (resource.source_ref or {}).get("name")
                    or ""
                )
                target_record = await _blocking(
                    executor.read_instance,
                    target_client,
                    project=row.target_project,
                    name=target_name,
                    label=f"migration.rollback_live_check[{target_name}]",
                )
                if target_record is None or str(
                    target_record.get("status") or ""
                ).lower() == "running":
                    raise MigrationConflict(
                        "Rollback requires a present, stopped target with no activation history."
                    )
            values = {"phase": "rolling_back", "status": "pending"}
        elif action == "restore_checkpoint":
            if not DESTRUCTIVE_RECOVERY_ENABLED:
                raise MigrationConflict(
                    "Checkpoint restore is disabled until authoritative target write history "
                    "can be proven."
                )
            if confirmation != "RESTORE":
                raise MigrationConflict("Type RESTORE to confirm checkpoint restoration.")
            if row.phase not in {"manual_recovery", "rollback_available"}:
                raise MigrationConflict("Checkpoint restore is unavailable in this phase.")
            if row.strategy == "member_relocation":
                raise MigrationConflict(
                    "Member relocation recovery requires a new reverse relocation plan."
                )
            resources = await repository.list_resources(self.db, row.job_id)
            instance_resources = [
                resource
                for resource in resources
                if resource.resource_type == "instance"
            ]
            if row.external_cutover_confirmed_at is not None or any(
                (resource.safe_result or {}).get("target_started") is not False
                or (resource.safe_result or {}).get("target_start_attempted") is not False
                for resource in instance_resources
            ):
                raise MigrationConflict(
                    "Checkpoint restore is blocked after target activation or ambiguous write "
                    "history; reverse sync is required."
                )
            if not any(
                (resource.safe_result or {}).get("checkpoint_name")
                for resource in instance_resources
            ):
                raise MigrationConflict("No durable checkpoint is available to restore.")
            for resource in instance_resources:
                target_name = str(
                    (resource.target_ref or {}).get("name")
                    or (resource.source_ref or {}).get("name")
                    or ""
                )
                target_record = await _blocking(
                    executor.read_instance,
                    target_client,
                    project=row.target_project,
                    name=target_name,
                    label=f"migration.restore_live_check[{target_name}]",
                )
                if target_record is not None:
                    raise MigrationConflict(
                        "Checkpoint restore requires the target to be absent and verified."
                    )
            values = {"phase": "preparing_recovery", "status": "pending"}
        elif action == "cleanup":
            if not DESTRUCTIVE_RECOVERY_ENABLED:
                raise MigrationConflict(
                    "Source cleanup is disabled until authoritative target write history can "
                    "be proven."
                )
            if confirmation != "CLEANUP":
                raise MigrationConflict("Type CLEANUP to confirm source deletion.")
            if row.phase not in {"cleanup_due", "cleanup_blocked"}:
                raise MigrationConflict("Source cleanup is unavailable in this phase.")
            if row.retention_deadline is None or row.retention_deadline > now:
                raise MigrationConflict("The source retention window has not ended.")
            values = {"phase": "cleaning_up", "status": "pending"}
        elif action == "recheck":
            if row.status not in {"unknown", "manual_recovery"}:
                raise MigrationConflict("Recheck is only available for unresolved migrations.")
            values = {
                "phase": "reconciling",
                "status": "pending",
                "locked_by": None,
                "lease_expires_at": None,
                "safe_result": _reset_reconciliation_cycle(row.safe_result),
            }
        else:
            raise MigrationConflict("Unsupported migration action.")
        action_authority = _authority_values(authority)
        # ``requester`` is the immutable job initiator and idempotency owner. The actor_* pin and
        # requester_role record the latest operator whose approved action the worker may resume;
        # the accepted event retains the initiator's original role independently.
        action_authority.pop("requester")
        values.update(action_authority)
        transitioned = await repository.set_job(
            self.db,
            row.job_id,
            terminal=terminal,
            event_message=f"Migration action '{action}' was accepted.",
            event_type=f"migration.{action}",
            event_actor=repository.event_actor(_authority_values(authority)),
            event_reason=reason,
            **values,
        )
        if not transitioned:
            raise MigrationConflict("The migration changed while applying the action.")
        updated = await repository.get_job(self.db, row.job_id)
        if updated is None:
            raise MigrationNotFound("Migration job not found.")
        if not terminal and values.get("status") == "pending":
            _schedule(run_migration_job(row.job_id, allow_mutation=True))
        return updated
