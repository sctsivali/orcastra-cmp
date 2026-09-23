"""Authoritative worker context, leases, and plan/resource reads."""

from __future__ import annotations

from typing import Any

from app.core.config import get_settings
from app.core.database import async_session_maker
from app.models.migration_models import MigrationJob, MigrationPlan, MigrationResource
from app.models.migration_schemas import MigrationPlanCreateRequest
from app.services.break_glass_service import cluster_fingerprint
from app.services.instance_job_identity import resolve_instance_job_organization
from app.services.lxd.availability import require_client
from app.services.lxd.connection import require_client_server_fingerprint
from app.services.lxd.exceptions import LXDTransportPinError
from app.services.lxd.lazy_client import client_for
from app.services.lxd_service import get_lxd_service
from app.services.snapshots.schedule_authority import standing_authority_refusal

from . import preflight, repository
from .authority import AuthentikAuthorityVerifier, CurrentAuthorityError
from .contracts import (
    MigrationAuthorityError,
    MigrationLeaseLost,
    _assert_job_plan_pin,
    _authority_pin,
    _blocking,
    _worker_fence,
)
from .plan_contracts import _preflight_payload, _target_pool
from .security import canonical_json_hash

async def _authoritative_clients(
    job: MigrationJob, *, current_authority: Any = None
) -> tuple[Any, Any]:
    if current_authority is None:
        try:
            current_authority = await AuthentikAuthorityVerifier().revalidate(
                _authority_pin(job)
            )
        except CurrentAuthorityError as exc:
            raise MigrationAuthorityError(str(exc), code=exc.code) from exc
    actor_email = current_authority.pin.identity.email
    actor_role = current_authority.role.value
    source_fingerprint = await cluster_fingerprint(job.source_cluster_id)
    target_fingerprint = (
        source_fingerprint
        if job.source_cluster_id == job.target_cluster_id
        else await cluster_fingerprint(job.target_cluster_id)
    )
    if source_fingerprint != job.source_cluster_fingerprint:
        raise MigrationAuthorityError("The source cluster registration changed.")
    if target_fingerprint != job.target_cluster_fingerprint:
        raise MigrationAuthorityError("The target cluster registration changed.")
    async with async_session_maker() as db:
        source_owner = await resolve_instance_job_organization(
            db, cluster_id=job.source_cluster_id, project=job.source_project
        )
        target_owner = await resolve_instance_job_organization(
            db, cluster_id=job.target_cluster_id, project=job.target_project
        )
        if (
            source_owner.resolution != "exact"
            or target_owner.resolution != "exact"
            or source_owner.organization_id != job.source_organization_id
            or target_owner.organization_id != job.target_organization_id
            or source_owner.organization_id != target_owner.organization_id
        ):
            raise MigrationAuthorityError("Project ownership changed or became ambiguous.")
        if actor_role == "partner":
            for cluster_id, project in (
                (job.source_cluster_id, job.source_project),
                (job.target_cluster_id, job.target_project),
            ):
                refusal = await standing_authority_refusal(
                    db,
                    actor_email=actor_email,
                    organization_id=job.source_organization_id,
                    cluster_id=cluster_id,
                    project=project,
                )
                if refusal is not None:
                    raise MigrationAuthorityError(refusal.message)
    lxd_service = get_lxd_service()
    try:
        source = require_client(
            await client_for(
                lxd_service,
                job.source_cluster_id,
                label="migration.worker.source",
                expected_server_fingerprint=job.source_cluster_fingerprint,
            ),
            job.source_cluster_id,
        )
        require_client_server_fingerprint(
            source, job.source_cluster_fingerprint
        )
        if job.source_cluster_id == job.target_cluster_id:
            if job.source_cluster_fingerprint != job.target_cluster_fingerprint:
                raise LXDTransportPinError(
                    "One cluster cannot have two approved certificate fingerprints."
                )
            return source, source
        target = require_client(
            await client_for(
                lxd_service,
                job.target_cluster_id,
                label="migration.worker.target",
                expected_server_fingerprint=job.target_cluster_fingerprint,
            ),
            job.target_cluster_id,
        )
        require_client_server_fingerprint(
            target, job.target_cluster_fingerprint
        )
        return source, target
    except LXDTransportPinError as exc:
        raise MigrationAuthorityError(
            "A cluster HTTPS transport is not bound to its approved certificate.",
            code="cluster_transport_pin_mismatch",
        ) from exc


async def _fenced_job_update(
    job: MigrationJob,
    worker_id: str,
    *,
    event_message: str,
    terminal: bool = False,
    **values: Any,
) -> bool:
    async with async_session_maker() as db:
        transitioned = await repository.set_job(
            db,
            job.job_id,
            worker_id=worker_id,
            fence_token=job.fence_token,
            event_message=event_message,
            event_actor=repository.event_actor(
                {
                    "requester_role": job.requester_role,
                    "actor_issuer": job.actor_issuer,
                    "actor_subject": job.actor_subject,
                    "actor_email": job.actor_email,
                    "actor_provider_uuid": job.actor_provider_uuid,
                }
            ),
            terminal=terminal,
            **values,
        )
    if not transitioned:
        raise MigrationLeaseLost("Migration lease ownership was lost.")
    return True


async def _resource_update(resource_id: str, **values: Any) -> None:
    fence = _worker_fence.get()
    if fence is None:
        raise MigrationLeaseLost("No migration lease is active for this resource update.")
    job_id, worker_id, fence_token = fence
    async with async_session_maker() as db:
        await repository.set_resource(
            db,
            resource_id,
            job_id=job_id,
            worker_id=worker_id,
            fence_token=fence_token,
            **values,
        )


async def _assert_mutation_lease() -> tuple[str, str, int]:
    """Fail immediately before a daemon mutation when this worker lost ownership."""
    fence = _worker_fence.get()
    if fence is None:
        raise MigrationLeaseLost("No migration mutation lease is active.")
    job_id, worker_id, fence_token = fence
    async with async_session_maker() as db:
        owned = await repository.assert_lease(db, job_id, worker_id, fence_token)
    if not owned:
        raise MigrationLeaseLost("Migration mutation lease expired or changed owner.")
    return fence


async def _job_plan(job: MigrationJob) -> MigrationPlan:
    async with async_session_maker() as db:
        row = await repository.get_plan(db, job.plan_id)
    if row is None:
        raise MigrationAuthorityError(
            "The pinned migration plan disappeared.",
            code="migration_plan_pin_mismatch",
        )
    _assert_job_plan_pin(job, row)
    return row


async def _resources(job_id: str) -> list[MigrationResource]:
    async with async_session_maker() as db:
        return await repository.list_resources(db, job_id)


async def _fresh_worker_preflight(
    job: MigrationJob, source_client: Any, target_client: Any
) -> dict[str, Any]:
    plan = await _job_plan(job)
    request = MigrationPlanCreateRequest.model_validate(plan.request_payload)
    result = await _blocking(
        preflight.inspect,
        label="migration.worker.preflight",
        scope=job.scope,
        intent=job.intent,
        source_client=source_client,
        target_client=target_client,
        source_cluster=job.source_cluster_id,
        source_project=job.source_project,
        source_name=job.source_resource_name,
        target_cluster=job.target_cluster_id,
        target_project=job.target_project,
        target_name=job.target_resource_name,
        target_member=job.target_member,
        target_pool=_target_pool(request),
        settings=get_settings(),
        mappings=request.mappings.model_dump(mode="json"),
    )
    report = _preflight_payload(result, request)

    def graph_signature(payload: dict[str, Any]) -> list[tuple[Any, ...]]:
        return sorted(
            (
                resource.get("resource_key"),
                resource.get("resource_type"),
                canonical_json_hash(
                    resource.get("source") or resource.get("source_name") or ""
                ),
                canonical_json_hash(
                    resource.get("target") or resource.get("target_name") or ""
                ),
                tuple(sorted(resource.get("dependency_keys") or [])),
            )
            for resource in payload.get("resources") or []
            if isinstance(resource, dict)
        )

    if graph_signature(report) != graph_signature(plan.preflight_payload or {}):
        report["blockers"].append(
            {
                "code": "resource_graph_changed",
                "severity": "blocker",
                "message": "The migration resource graph changed after execution approval.",
                "remediation": "Create and approve a new migration plan from current inventory.",
            }
        )
    return report


async def _wait_for_cutover(job: MigrationJob, worker_id: str) -> None:
    await _fenced_job_update(
        job,
        worker_id,
        phase="cutover_pending",
        status="waiting",
        locked_by=None,
        lease_expires_at=None,
        event_message="Preflight passed. Migration is waiting for cutover approval.",
    )
