"""Authorized operator transitions for durable migration jobs."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

from sqlalchemy import select

from app.core.org_scope import get_user_org_ids
from app.models.db_models import InstanceOperationJob, RebuildJob
from app.models.migration_models import MigrationJob
from app.services.shared_cluster_audit import serialize_cluster_configuration

from . import repository
from .authority import AuthentikAuthorityVerifier
from .context import _authoritative_clients, _job_plan, _prove_job_authority
from .contracts import (
    MigrationAuthorityError,
    MigrationConflict,
    MigrationNotFound,
    _authority_values,
)
from .project_retention_store import fence_project_for_recovery
from .retention_store import RETAINED_PHASES, refence_target_for_rollback
from .security import sanitize_payload
from .worker import _schedule, run_migration_job


# Actions that hand daemon work to the worker or fence a cluster now, so they prove both clusters
# and their registrations at request time. Release, Cancel and Recheck change no daemon here: a
# stuck job on a cluster that is gone for good must stay releasable, and Recheck's worker run
# proves everything again itself.
_DAEMON_ACTIONS = frozenset({"cutover", "confirm_external_cutover", "rollback", "cleanup"})


def _reset_reconciliation_cycle(value: Any) -> dict[str, Any]:
    state = dict(value) if isinstance(value, dict) else {}
    raw_cycle = state.get("reconciliation_cycle", 0)
    cycle = raw_cycle if isinstance(raw_cycle, int) and not isinstance(raw_cycle, bool) else 0
    state["reconciliation_cycle"] = max(0, cycle) + 1
    state["reconciliation_streak"] = 0
    state.pop("reconciliation_retry_delay_seconds", None)
    return sanitize_payload(state)


def _has_retained_source(row: MigrationJob) -> bool:
    """A staged instance move or a project relocation: both keep the source for rollback."""
    if row.intent != "move" or row.strategy == "member_relocation":
        return False
    return row.scope == "instance" or row.strategy == "project_relocation"


async def _target_has_active_work(db: Any, row: MigrationJob) -> bool:
    """Ordinary instance or rebuild work on the target would race the rollback delete."""
    target_name = row.target_resource_name or row.source_resource_name
    for model, statuses in (
        (InstanceOperationJob, ("pending", "running", "unknown")),
        (RebuildJob, ("pending", "stopping", "rebuilding", "starting", "provisioning")),
    ):
        # A project rollback deletes every instance of the target project.
        scope = (
            [model.instance_name == target_name] if row.scope == "instance" else []
        )
        statement = (
            select(model.job_id)
            .where(
                model.cluster_id == row.target_cluster_id,
                model.project == row.target_project,
                *scope,
                model.status.in_(statuses),
            )
            .limit(1)
        )
        if (await db.execute(statement)).first() is not None:
            return True
    return False


async def _fence_project(db: Any, row: MigrationJob, *, side: str) -> None:
    """Fence the project a project rollback or Finalize tears down, in the action's transaction."""
    try:
        await fence_project_for_recovery(db, row, side=side)
    except repository.RepositoryConflict as exc:
        raise MigrationConflict(str(exc)) from exc


class MigrationActionsMixin:
    def _require_recovery_actions(self) -> None:
        if not bool(getattr(self.settings, "migration_recovery_actions_enabled", True)):
            raise MigrationConflict("Rollback and source cleanup are disabled on this deployment.")

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
        # important: every external call (identity provider, certificate store, both daemons)
        # runs before the row lock. Holding FOR UPDATE across them stalled the worker's heartbeat
        # behind a slow answer, and repeated requests could pile up on the lock.
        row = await repository.get_job(self.db, job_id)
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
        if action in _DAEMON_ACTIONS:
            # Request ContextVars do not authorize a durable action. Rebuild ownership,
            # organization affiliation, registration fingerprints, and daemon identity now.
            await _authoritative_clients(
                row, current_authority=authority
            )
        elif action != "release":
            # Cancel and Recheck change no daemon here, but still need current ownership and
            # standing authority from the database. Release stays admin-only and needs neither,
            # so a job on a decommissioned or deregistered cluster can always be released.
            await _prove_job_authority(row, authority)
        row = await repository.get_job(self.db, job_id, lock=True)
        if row is None:
            raise MigrationNotFound("Migration job not found.")
        if row.revision != expected_revision:
            raise MigrationConflict("The migration changed. Reload it before acting.")
        now = repository.utcnow()
        values: dict[str, Any]
        terminal = False
        if action == "cancel":
            if row.phase not in {"queued", "cutover_pending"}:
                raise MigrationConflict("This phase can no longer be cancelled safely.")
            # note: a retry delay is a lease nobody holds (locked_by is None), not a worker.
            if row.status == "running" or (
                row.locked_by is not None
                and row.lease_expires_at is not None
                and row.lease_expires_at > now
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
            self._require_recovery_actions()
            if confirmation != "ROLLBACK":
                raise MigrationConflict("Type ROLLBACK to confirm the rollback.")
            if not _has_retained_source(row):
                raise MigrationConflict(
                    "Only a staged move or a project relocation has a retained source to roll "
                    "back to."
                )
            if row.phase not in RETAINED_PHASES or row.status != "waiting":
                raise MigrationConflict("Rollback is only available while the source is retained.")
            # important: the same cluster lock ordinary instance writes take before their fence
            # check, so no write admitted before the re-fence can land after it.
            async with serialize_cluster_configuration(self.db, row.target_cluster_id):
                if await _target_has_active_work(self.db, row):
                    raise MigrationConflict(
                        "The target has an operation in progress. Retry once it finishes."
                    )
                if row.scope == "instance":
                    try:
                        await refence_target_for_rollback(self.db, row)
                    except repository.RepositoryConflict as exc:
                        raise MigrationConflict(str(exc)) from exc
                else:
                    await _fence_project(self.db, row, side="target")
            values = {
                "phase": "rolling_back",
                "status": "pending",
                "locked_by": None,
                "lease_expires_at": None,
                "safe_result": sanitize_payload(
                    {**(row.safe_result or {}), "recovery_kind": "rollback"}
                ),
            }
        elif action == "restore_checkpoint":
            raise MigrationConflict(
                "Checkpoint restore is not offered. Roll back to the retained source instead."
            )
        elif action == "cleanup":
            self._require_recovery_actions()
            if confirmation != "CLEANUP":
                raise MigrationConflict("Type CLEANUP to confirm deleting the retained source.")
            if not _has_retained_source(row):
                raise MigrationConflict("This migration has no retained source to delete.")
            if row.phase not in RETAINED_PHASES or row.status != "waiting":
                raise MigrationConflict("The retained source can only be deleted during retention.")
            if row.scope == "project":
                async with serialize_cluster_configuration(self.db, row.source_cluster_id):
                    await _fence_project(self.db, row, side="source")
            values = {
                "phase": "cleaning_up",
                "status": "pending",
                "locked_by": None,
                "lease_expires_at": None,
                "error_code": None,
                "error_message": None,
                "safe_result": sanitize_payload(
                    {
                        **(row.safe_result or {}),
                        "cleanup_trigger": "finalize",
                        "recovery_kind": "cleanup",
                    }
                ),
            }
        elif action == "release":
            if requester_role != "admin":
                raise MigrationAuthorityError(
                    "Only an administrator can release a migration.", code="current_role_denied"
                )
            if confirmation != "RELEASE":
                raise MigrationConflict("Type RELEASE to end the migration without changes.")
            if row.phase not in {"cleanup_blocked", "manual_recovery"} or row.status not in {
                "waiting",
                "manual_recovery",
            }:
                raise MigrationConflict("Only a blocked or manual-recovery migration can be released.")
            retained = row.phase == "cleanup_blocked"
            values = {
                "phase": "completed" if retained else "failed",
                "status": "success" if retained else "failure",
                "safe_result": sanitize_payload(
                    {**(row.safe_result or {}), "released": True, "release_reason": reason}
                ),
            }
            terminal = True
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
            expected_revision=expected_revision,
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
