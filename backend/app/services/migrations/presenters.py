"""Sanitized public projections for durable migration records."""

from __future__ import annotations

from typing import Any

from app.models.migration_models import MigrationAuditOutbox, MigrationJob, MigrationPlan
from app.models.migration_schemas import (
    MigrationActionName,
    MigrationEndpointRef,
    MigrationIssue,
    MigrationJobResponse,
    MigrationMappings,
    MigrationPlanResponse,
    MigrationResourceResponse,
    MigrationTimelineEvent,
)

from . import repository
from .security import sanitize_error, sanitize_payload


def _endpoint(row: Any, side: str) -> MigrationEndpointRef:
    return MigrationEndpointRef(
        cluster_id=getattr(row, f"{side}_cluster_id"),
        project=getattr(row, f"{side}_project"),
        resource_name=getattr(row, f"{side}_resource_name"),
        member=getattr(row, f"{side}_member"),
        cluster_fingerprint=getattr(row, f"{side}_cluster_fingerprint"),
    )


def plan_response(row: MigrationPlan) -> MigrationPlanResponse:
    preflight = dict(row.preflight_payload or {})
    request = dict(row.request_payload or {})
    effective_status = row.status
    if row.expires_at <= repository.utcnow() and effective_status not in {"executed", "expired"}:
        effective_status = "expired"
    return MigrationPlanResponse(
        plan_id=row.plan_id,
        revision=row.revision,
        scope=row.scope,
        intent=row.intent,
        status=effective_status,
        source=_endpoint(row, "source"),
        target=_endpoint(row, "target"),
        strategy=preflight.get("strategy"),
        downtime_class=preflight.get("downtime_class") or "unknown",
        retention_hours=row.retention_hours,
        checkpoint_requested=row.checkpoint_requested,
        external_cutover_required=row.external_cutover_required,
        capabilities=preflight.get("capabilities") or [],
        resources=preflight.get("resources") or [],
        mappings=MigrationMappings.model_validate(request.get("mappings") or {}),
        blockers=[MigrationIssue.model_validate(issue) for issue in preflight.get("blockers") or []],
        warnings=[MigrationIssue.model_validate(issue) for issue in preflight.get("warnings") or []],
        rollback_contract=sanitize_payload(preflight.get("rollback_contract") or {}),
        expires_at=row.expires_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _timeline_event(row: MigrationAuditOutbox, fallback_phase: str) -> MigrationTimelineEvent:
    payload = dict(row.payload or {})
    return MigrationTimelineEvent(
        sequence=row.sequence,
        phase=str(payload.get("phase") or fallback_phase),
        message=sanitize_error(payload.get("message") or "Migration state changed.", limit=2000),
        created_at=row.created_at,
    )


def _available_actions(job: MigrationJob, resources: list[Any]) -> list[MigrationActionName]:
    actions: list[MigrationActionName] = []
    no_mutation = all(
        not resource.operation_refs and resource.state in {"pending", "blocked"}
        for resource in resources
    )
    if (
        job.phase == "queued"
        and job.status == "pending"
        and job.lease_expires_at is None
        and no_mutation
    ) or (
        job.phase == "cutover_pending" and job.status == "waiting" and no_mutation
    ):
        actions.append(MigrationActionName.CANCEL)
    if job.phase == "cutover_pending" and job.status == "waiting":
        actions.append(MigrationActionName.CUTOVER)
    if job.phase == "external_cutover_pending" and job.status == "waiting":
        actions.append(MigrationActionName.CONFIRM_EXTERNAL_CUTOVER)
    # Destructive recovery is intentionally absent until daemon-side target write history can
    # be proven authoritatively. The action endpoint independently enforces the same gate.
    if job.status in {"unknown", "manual_recovery"}:
        actions.append(MigrationActionName.RECHECK)
    return actions


async def job_response(db: Any, row: MigrationJob) -> MigrationJobResponse:
    resources = await repository.list_resources(db, row.job_id)
    timeline = await repository.list_timeline(db, row.job_id)
    return MigrationJobResponse(
        job_id=row.job_id,
        plan_id=row.plan_id,
        plan_revision=row.plan_revision,
        revision=row.revision,
        scope=row.scope,
        intent=row.intent,
        source=_endpoint(row, "source"),
        target=_endpoint(row, "target"),
        strategy=row.strategy,
        downtime_class=row.downtime_class,
        phase=row.phase,
        status=row.status,
        retention_hours=row.retention_hours,
        retention_deadline=row.retention_deadline,
        external_cutover_required=row.external_cutover_required,
        external_cutover_confirmed_at=row.external_cutover_confirmed_at,
        resources=[
            MigrationResourceResponse(
                resource_id=resource.resource_id,
                resource_key=resource.resource_key,
                resource_type=resource.resource_type,
                ordinal=resource.ordinal,
                state=resource.state,
                source=sanitize_payload(resource.source_ref or {}),
                target=sanitize_payload(resource.target_ref or {}),
                dependency_keys=list(resource.dependency_keys or []),
                operation_refs=sanitize_payload(resource.operation_refs or []),
                progress=sanitize_payload(resource.progress or {}),
                error_code=resource.error_code,
                error_message=(
                    sanitize_error(resource.error_message, limit=2000)
                    if resource.error_message
                    else None
                ),
                updated_at=resource.updated_at,
                finished_at=resource.finished_at,
            )
            for resource in resources
        ],
        timeline=[_timeline_event(event, row.phase) for event in timeline],
        available_actions=_available_actions(row, resources),
        safe_result=sanitize_payload(row.safe_result) if row.safe_result else None,
        error_code=row.error_code,
        error_message=(sanitize_error(row.error_message, limit=2000) if row.error_message else None),
        created_at=row.created_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


__all__ = ["job_response", "plan_response"]
