"""Migration draft planning, preflight, and execution acceptance."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.org_scope import get_user_org_ids
from app.models.db_models import (
    ImageUploadSession,
    InstanceOperationJob,
    IsoUploadSession,
    RebuildJob,
)
from app.models.migration_models import MigrationJob, MigrationPlan
from app.models.migration_schemas import MigrationPlanCreateRequest, MigrationPlanPatchRequest
from app.services.break_glass_service import (
    PinStoreUnavailable,
    cluster_pin_state,
)
from app.services.instance_job_identity import resolve_instance_job_organization
from app.services.lxd.availability import require_client
from app.services.lxd.connection import require_client_server_fingerprint
from app.services.lxd.exceptions import LXDTransportPinError
from app.services.lxd_certs.pinning import LXDCertificatePinError
from app.services.lxd.lazy_client import client_for, refresh_pin_for
from app.services.lxd_service import LXDService, get_lxd_service
from app.services.shared_cluster_audit import serialize_cluster_configuration

from . import preflight, project_policy, repository
from .authority import AuthentikAuthorityVerifier
from .billing import (
    enforce_migration_billing_policy,
    report_has_running_source,
    report_needs_unmeasured_size_consent,
    validate_acknowledgements,
)
from .contracts import (
    MigrationAuthorityError,
    MigrationConflict,
    MigrationNotFound,
    _authority_pin,
    _authority_values,
    _blocking,
    _enum_value,
)
from .plan_contracts import _preflight_payload, _target_pool, approval_report_hash
from .planning_checks import (
    active_endpoint_blockers,
    orcahub_blockers,
    project_lock_blockers,
    shared_policy_blockers,
    single_owner_clusters,
    stale_target_access_blockers,
)
from .security import canonical_json_hash, canonical_resource_key, sanitize_payload
from .worker import _schedule, run_migration_job


def _require_plan_actor(row: Any, authority: Any) -> None:
    """Conceal plans not pinned to the exact current Authentik actor."""
    try:
        actor_pin = _authority_pin(row)
    except MigrationAuthorityError as exc:
        raise MigrationNotFound("Migration plan not found.") from exc
    if actor_pin != authority.pin:
        raise MigrationNotFound("Migration plan not found.")


@asynccontextmanager
async def _serialize_migration_clusters(
    db: AsyncSession,
    cluster_ids: Iterable[str],
) -> AsyncIterator[None]:
    """Acquire every participating cluster lock once in a deterministic order."""
    async with AsyncExitStack() as stack:
        for cluster_id in sorted(set(cluster_ids)):
            await stack.enter_async_context(
                serialize_cluster_configuration(db, cluster_id)
            )
        yield


async def _has_active_instance_operation(
    db: AsyncSession,
    request: MigrationPlanCreateRequest,
) -> bool:
    """Return whether durable ordinary work can still mutate either endpoint.

    Admission for create, specification, and rebuild jobs is serialized on the same cluster
    advisory lock as final migration preflight. Once this query runs under that lock, no new
    relevant job can be committed before migration resource fences are committed. Existing
    active rows remain hard blockers because an unknown or partially applied job may resume.
    """

    scope = _enum_value(request.scope)
    source_name = request.source.resource_name
    target_name = request.target.resource_name or source_name

    def endpoint(
        cluster_id: str,
        project: str,
        resource_name: Optional[str],
    ) -> Any:
        clauses = [
            InstanceOperationJob.cluster_id == cluster_id,
            InstanceOperationJob.project == project,
        ]
        if scope == "instance":
            # An instance migration always has a source name. Keep the predicate fail-closed if
            # a malformed internal request ever reaches this boundary.
            clauses.append(
                InstanceOperationJob.instance_name
                == (resource_name or "__missing_migration_resource__")
            )
        return and_(*clauses)

    operation_statement = (
        select(InstanceOperationJob.job_id)
        .where(
            InstanceOperationJob.status.in_(("pending", "running", "unknown")),
            or_(
                endpoint(
                    request.source.cluster_id,
                    request.source.project,
                    source_name,
                ),
                endpoint(
                    request.target.cluster_id,
                    request.target.project,
                    target_name,
                ),
            ),
        )
        .limit(1)
    )
    if (await db.execute(operation_statement)).first() is not None:
        return True

    # Rebuild orchestration is a separate durable table. Its worker holds the mutation guard,
    # but a backend crash releases the advisory lock while the accepted daemon rebuild may
    # still be running. The durable in-flight row therefore remains a blocker until recovery
    # marks it ready or failed.
    source_rebuild_scope = [
        RebuildJob.cluster_id == request.source.cluster_id,
        RebuildJob.project == request.source.project,
    ]
    target_rebuild_scope = [
        RebuildJob.cluster_id == request.target.cluster_id,
        RebuildJob.project == request.target.project,
    ]
    if scope == "instance":
        source_rebuild_scope.append(
            RebuildJob.instance_name
            == (source_name or "__missing_migration_resource__")
        )
        target_rebuild_scope.append(
            RebuildJob.instance_name
            == (target_name or "__missing_migration_resource__")
        )
    rebuild_statement = (
        select(RebuildJob.job_id)
        .where(
            RebuildJob.status.in_(
                ("pending", "stopping", "rebuilding", "starting", "provisioning")
            ),
            or_(
                and_(*source_rebuild_scope),
                and_(*target_rebuild_scope),
            ),
        )
        .limit(1)
    )
    return (await db.execute(rebuild_statement)).first() is not None


async def _has_active_upload_session(
    db: AsyncSession,
    request: MigrationPlanCreateRequest,
) -> bool:
    """Return whether either project has upload work that can still reach the daemon.

    Upload admission commits under the same cluster advisory lock as final migration preflight.
    Once this query runs under that lock, an active row is authoritative and no new row can appear
    before migration fences are committed. ``unknown`` is intentionally included even though it
    is a recovery-only state: uncertainty after possible daemon submission is active work, never
    evidence that the project is idle.
    """
    active_statuses = ("pending", "uploading", "uploaded", "importing", "unknown")

    def endpoint(model: Any, cluster_id: str, project: str) -> Any:
        return and_(model.cluster_id == cluster_id, model.project == project)

    def endpoint_filter(model: Any) -> Any:
        return or_(
            endpoint(model, request.source.cluster_id, request.source.project),
            endpoint(model, request.target.cluster_id, request.target.project),
        )
    for model in (ImageUploadSession, IsoUploadSession):
        statement = (
            select(model.session_id)
            .where(
                model.status.in_(active_statuses),
                endpoint_filter(model),
            )
            .limit(1)
        )
        if (await db.execute(statement)).first() is not None:
            return True
    return False


def _target_project_config(client: Any, project: str) -> dict[str, Any]:
    record = preflight._get_project(client, project)
    return dict((record or {}).get("config") or {})



def _cluster_label(cluster_id: str) -> str:
    """The name an operator knows the cluster by, the id when no display name is set."""
    try:
        from app.core.vault_client import get_vault_client
        from app.services.lxd.status import resolve_display_name

        settings = get_settings()
        vault_name = None
        if settings.vault_enabled and settings.vault_token:
            metadata = get_vault_client(settings.vault_addr, settings.vault_token).vault_read(
                f"{settings.vault_clusters_path}/{cluster_id}/metadata"
            )
            vault_name = (metadata or {}).get("display_name")
        return resolve_display_name(cluster_id, vault_name)
    except Exception:  # noqa: BLE001
        return cluster_id

class MigrationPlanningMixin:
    def __init__(
        self,
        db: AsyncSession,
        lxd_service: Optional[LXDService] = None,
        settings: Any = None,
    ) -> None:
        self.db = db
        self.lxd_service = lxd_service or get_lxd_service()
        self.settings = settings or get_settings()

    async def _clients(
        self,
        request: MigrationPlanCreateRequest,
        *,
        source_fingerprint: str,
        target_fingerprint: str,
    ) -> tuple[Any, Any]:
        try:
            source = require_client(
                await client_for(
                    self.lxd_service,
                    request.source.cluster_id,
                    label="migration.source",
                    expected_server_fingerprint=source_fingerprint,
                ),
                request.source.cluster_id,
            )
            require_client_server_fingerprint(source, source_fingerprint)
            if request.target.cluster_id == request.source.cluster_id:
                if source_fingerprint != target_fingerprint:
                    raise LXDTransportPinError(
                        "One cluster cannot have two approved certificate fingerprints."
                    )
                return source, source
            target = require_client(
                await client_for(
                    self.lxd_service,
                    request.target.cluster_id,
                    label="migration.target",
                    expected_server_fingerprint=target_fingerprint,
                ),
                request.target.cluster_id,
            )
            require_client_server_fingerprint(target, target_fingerprint)
            return source, target
        except LXDTransportPinError as exc:
            raise MigrationConflict(
                "A cluster HTTPS transport is not bound to its approved certificate."
            ) from exc

    async def _organizations(
        self,
        request: MigrationPlanCreateRequest,
        *,
        user: Any,
        requester_role: str,
    ) -> tuple[int, int]:
        source = await resolve_instance_job_organization(
            self.db,
            cluster_id=request.source.cluster_id,
            project=request.source.project,
        )
        target = await resolve_instance_job_organization(
            self.db,
            cluster_id=request.target.cluster_id,
            project=request.target.project,
        )
        if source.resolution != "exact" or source.organization_id is None:
            raise MigrationConflict(
                "Source project ownership is ambiguous. Migration fails closed on shared clusters."
            )
        if target.resolution != "exact" or target.organization_id is None:
            raise MigrationConflict(
                "Target project ownership is ambiguous. Bind it to one organization first."
            )
        if source.organization_id != target.organization_id:
            raise MigrationConflict("Source and target must belong to the same organization.")
        relocation = _enum_value(request.scope) == "project" and (
            request.source.cluster_id,
            request.source.project,
        ) != (request.target.cluster_id, request.target.project)
        if relocation and not await single_owner_clusters(
            self.db, request.source.cluster_id, request.target.cluster_id
        ):
            # note: ownership of a project that does not exist yet can only be proven through
            # the cluster binding. Shared clusters wait for a staged ownership grant.
            raise MigrationConflict(
                "A whole project can only be relocated between clusters that each belong to one "
                "organization. Move its instances individually on a shared cluster."
            )
        if requester_role == "partner":
            memberships = set(await get_user_org_ids(user, self.db))
            if source.organization_id not in memberships:
                raise MigrationAuthorityError(
                    "The operator is no longer affiliated with the migration organization."
                )
        return source.organization_id, target.organization_id

    async def _pin_state(self, cluster_id: str):
        """The cluster's current pin, read again from the certificate the cluster presents now.

        note: one identifying handshake per cluster per plan action. It records a pin for a
        reachable cluster that has none, and a public-CA renewal that no connection has noticed
        yet, so the transports below bind to what the cluster actually serves. It sends no client
        credential, so it is safe for a cluster that has never been proven.
        """
        try:
            await cluster_pin_state(cluster_id, strict=True)
        except PinStoreUnavailable as exc:
            raise MigrationConflict(
                "The certificate store could not be read, so the clusters' identity cannot be "
                "proven right now. Try again in a moment."
            ) from exc
        try:
            await refresh_pin_for(self.lxd_service, cluster_id)
        except LXDCertificatePinError as exc:
            label = await asyncio.to_thread(_cluster_label, cluster_id)
            raise MigrationConflict(
                f"{label} presented a different server certificate than the one pinned. "
                "An Administrator has to re-pin it before it can take part in a migration."
            ) from exc
        return await cluster_pin_state(cluster_id)

    async def _fingerprints(
        self, request: MigrationPlanCreateRequest
    ) -> tuple[str, str]:
        source = await self._pin_state(request.source.cluster_id)
        target = (
            source
            if request.source.cluster_id == request.target.cluster_id
            else await self._pin_state(request.target.cluster_id)
        )
        missing = [
            cluster_id
            for cluster_id, state in (
                (request.source.cluster_id, source),
                (request.target.cluster_id, target),
            )
            if state is None
        ]
        if missing and not self.settings.lxd_pin_server_certificate:
            raise MigrationConflict(
                "Certificate pinning is turned off on this deployment, so Migration Center cannot "
                "prove which clusters it is talking to."
            )
        if missing:
            labels = [await asyncio.to_thread(_cluster_label, cluster_id) for cluster_id in missing]
            names = " and ".join(dict.fromkeys(labels))
            raise MigrationConflict(
                f"{names} has no recorded server certificate yet, so its identity cannot be "
                "proven. Check that the cluster is reachable and try again, or ask an "
                "Administrator to re-pin its certificate."
            )
        if request.source.cluster_fingerprint and not source.accepts(
            request.source.cluster_fingerprint
        ):
            raise MigrationConflict("The source cluster registration changed. Re-select it.")
        if request.target.cluster_fingerprint and not target.accepts(
            request.target.cluster_fingerprint
        ):
            raise MigrationConflict("The target cluster registration changed. Re-select it.")
        return source.fingerprint, target.fingerprint

    async def _renewed_since(self, request: MigrationPlanCreateRequest, plan: Any) -> bool:
        """Whether every pin that differs from the plan's is a public-CA renewal of it."""
        for cluster_id, approved in (
            (request.source.cluster_id, plan.source_cluster_fingerprint),
            (request.target.cluster_id, plan.target_cluster_fingerprint),
        ):
            state = await cluster_pin_state(cluster_id)
            if state is None or not state.accepts(approved):
                return False
        return True

    async def _preflight(
        self,
        request: MigrationPlanCreateRequest,
        *,
        source_fingerprint: str,
        target_fingerprint: str,
        user: Any = None,
        organization_id: Optional[int] = None,
    ) -> dict[str, Any]:
        source_client, target_client = await self._clients(
            request,
            source_fingerprint=source_fingerprint,
            target_fingerprint=target_fingerprint,
        )
        result = await _blocking(
            preflight.inspect,
            label="migration.preflight",
            scope=_enum_value(request.scope),
            intent=_enum_value(request.intent),
            source_client=source_client,
            target_client=target_client,
            source_cluster=request.source.cluster_id,
            source_project=request.source.project,
            source_name=request.source.resource_name,
            target_cluster=request.target.cluster_id,
            target_project=request.target.project,
            target_name=request.target.resource_name,
            target_member=request.target.member,
            target_pool=_target_pool(request),
            settings=self.settings,
            mappings=request.mappings.model_dump(mode="json"),
        )
        payload = _preflight_payload(result, request)
        checkpoint_blocker: Optional[dict[str, str]] = None
        if result.strategy == "member_relocation":
            checkpoint_blocker = {
                "code": "member_relocation_checkpoint_unsupported",
                "severity": "blocker",
                "message": "Automatic checkpoints are not supported for member relocation.",
                "remediation": (
                    "Disable the migration checkpoint. Create and validate any required manual "
                    "snapshot under a separately reviewed recovery procedure."
                ),
            }
            checkpoint_reason = (
                "Member relocation checkpoints are not retained by the current recovery contract."
            )
        elif _enum_value(request.intent) == "move" and _enum_value(request.scope) == "instance":
            checkpoint_blocker = {
                "code": "move_checkpoint_unsupported",
                "severity": "blocker",
                "message": "Automatic checkpoints are not used for an instance move.",
                "remediation": (
                    "Disable the checkpoint. The stopped source is retained for the whole "
                    "retention window and is the rollback point."
                ),
            }
            checkpoint_reason = "The retained source is the rollback point for an instance move."
        elif _enum_value(request.intent) == "copy":
            checkpoint_blocker = {
                "code": "copy_checkpoint_unsupported",
                "severity": "blocker",
                "message": "Automatic checkpoints are not supported for durable copy.",
                "remediation": (
                    "Disable the migration checkpoint. The copy includes the source's existing "
                    "snapshot inventory but does not create a new checkpoint."
                ),
            }
            checkpoint_reason = "Durable copy does not create a new source checkpoint."
        if checkpoint_blocker is not None:
            payload["capabilities"] = [
                capability
                for capability in payload["capabilities"]
                if capability.get("code") != "checkpoint"
            ]
            payload["capabilities"].append(
                {
                    "code": "checkpoint",
                    "supported": False,
                    "reason": checkpoint_reason,
                }
            )
            payload["daemon_capabilities"]["checkpoint"] = False
            if request.checkpoint_requested:
                payload["blockers"].append(checkpoint_blocker)
        if await _has_active_instance_operation(self.db, request):
            payload["blockers"].append(
                {
                    "code": "active_instance_operation",
                    "severity": "blocker",
                    "message": (
                        "A durable create, specification, or rebuild job can still mutate the "
                        "source or destination."
                    ),
                    "remediation": (
                        "Wait for the instance operation to reach a terminal state. Reconcile "
                        "an unknown job before running migration preflight again."
                    ),
                }
            )
        if await _has_active_upload_session(self.db, request):
            payload["blockers"].append(
                {
                    "code": "active_upload_session",
                    "severity": "blocker",
                    "message": (
                        "An image or ISO upload can still mutate the source or destination "
                        "project."
                    ),
                    "remediation": (
                        "Finish or cancel every active upload session. Reconcile an unknown "
                        "import before running migration preflight again."
                    ),
                }
            )
        payload["blockers"].extend(
            await orcahub_blockers(
                self.db, request, strategy=result.strategy, settings=self.settings
            )
        )
        payload["blockers"].extend(await active_endpoint_blockers(self.db, request))
        payload["blockers"].extend(await project_lock_blockers(self.db, request))
        payload["blockers"].extend(
            await stale_target_access_blockers(self.db, request, organization_id)
        )
        if result.contracts and (
            _enum_value(request.scope) == "instance" or result.strategy == "project_relocation"
        ):
            creates_project = bool(result.capabilities.get("target_project_create"))
            target_project_config = None
            if result.capabilities.get("target_project_exists"):
                target_project_config = await _blocking(
                    _target_project_config,
                    target_client,
                    request.target.project,
                    label="migration.target_project_policy",
                )
            elif creates_project:
                # The worker creates the target as a clone of the source project.
                source_project_config = await _blocking(
                    _target_project_config,
                    source_client,
                    request.source.project,
                    label="migration.source_project_policy",
                )
                target_project_config = project_policy.clone_config(
                    {"config": source_project_config}, job_id=None
                )
            payload["blockers"].extend(
                await shared_policy_blockers(
                    self.db,
                    user=user,
                    request=request,
                    contracts=result.contracts,
                    target_project_config=target_project_config,
                    creates_project=creates_project,
                )
            )
        if request.external_cutover_required:
            payload["blockers"].append(
                {
                    "code": "signed_cutover_webhook_unavailable",
                    "severity": "blocker",
                    "message": "Signed external cutover delivery is not enabled in this release.",
                    "remediation": (
                        "Disable external cutover for this plan or configure the future durable "
                        "signed-webhook stage. Callback URLs are never accepted from requests."
                    ),
                }
            )
        payload["approval_hash"] = approval_report_hash(payload)
        return payload

    def _require_execution_stage(
        self, request: MigrationPlanCreateRequest, report: dict[str, Any]
    ) -> None:
        scope = _enum_value(request.scope)
        intent = _enum_value(request.intent)
        if scope == "instance":
            enabled = (
                self.settings.migration_durable_copy_enabled
                if intent == "copy"
                else self.settings.migration_instance_move_enabled
            )
        elif report.get("strategy") == "member_relocation":
            enabled = bool(
                self.settings.project_relocation_enabled
                and self.settings.migration_project_member_relocation_enabled
            )
        else:
            enabled = bool(
                self.settings.project_relocation_enabled
                and self.settings.migration_project_cross_cluster_enabled
            )
        if not enabled:
            raise MigrationConflict(
                "This migration execution stage is disabled; read-only preflight remains available."
            )

    async def create_plan(
        self,
        request: MigrationPlanCreateRequest,
        *,
        user: Any,
        requester_role: str,
    ) -> MigrationPlan:
        authority = await AuthentikAuthorityVerifier(self.settings).pin_user(user)
        requester_role = authority.role.value
        source_org, target_org = await self._organizations(
            request, user=user, requester_role=requester_role
        )
        source_fingerprint, target_fingerprint = await self._fingerprints(request)
        report = await self._preflight(
            request,
            source_fingerprint=source_fingerprint,
            target_fingerprint=target_fingerprint,
            user=user,
            organization_id=source_org,
        )
        payload = request.model_dump(mode="json")
        payload["source"]["cluster_fingerprint"] = source_fingerprint
        payload["target"]["cluster_fingerprint"] = target_fingerprint
        source_name = request.source.resource_name
        target_name = request.target.resource_name or source_name
        return await repository.insert_plan(
            self.db,
            scope=_enum_value(request.scope),
            intent=_enum_value(request.intent),
            status="ready" if not report["blockers"] else "blocked",
            **_authority_values(authority),
            source_organization_id=source_org,
            target_organization_id=target_org,
            source_cluster_id=request.source.cluster_id,
            source_project=request.source.project,
            source_resource_name=source_name,
            source_member=request.source.member,
            source_cluster_fingerprint=source_fingerprint,
            target_cluster_id=request.target.cluster_id,
            target_project=request.target.project,
            target_resource_name=target_name,
            target_member=request.target.member,
            target_cluster_fingerprint=target_fingerprint,
            source_key=canonical_resource_key(
                _enum_value(request.scope),
                request.source.cluster_id,
                request.source.project,
                source_name,
            ),
            target_key=canonical_resource_key(
                _enum_value(request.scope),
                request.target.cluster_id,
                request.target.project,
                target_name,
            ),
            payload_hash=canonical_json_hash(payload),
            request_payload=sanitize_payload(payload),
            preflight_payload=report,
            retention_hours=request.retention_hours,
            checkpoint_requested=request.checkpoint_requested,
            external_cutover_required=request.external_cutover_required,
        )

    async def get_plan(self, plan_id: str) -> MigrationPlan:
        row = await repository.get_plan(self.db, plan_id)
        if row is None:
            raise MigrationNotFound("Migration plan not found.")
        if row.expires_at <= repository.utcnow() and row.status not in {"executed", "expired"}:
            row.status = "expired"
        return row

    async def list_plans(
        self,
        *,
        visibility: repository.MigrationListVisibility,
        limit: int,
        offset: int,
    ) -> tuple[list[MigrationPlan], int]:
        return await repository.list_plans(
            self.db,
            visibility,
            limit=limit,
            offset=offset,
        )

    async def patch_plan(
        self,
        plan_id: str,
        patch: MigrationPlanPatchRequest,
        *,
        expected_revision: int,
        user: Any,
        requester_role: str,
    ) -> MigrationPlan:
        row = await repository.get_plan(self.db, plan_id, lock=True)
        if row is None:
            raise MigrationNotFound("Migration plan not found.")
        authority = await AuthentikAuthorityVerifier(self.settings).pin_user(user)
        _require_plan_actor(row, authority)
        merged = dict(row.request_payload or {})
        requester_role = authority.role.value
        updates = patch.model_dump(mode="json", exclude_unset=True)
        merged.update(updates)
        request = MigrationPlanCreateRequest.model_validate(merged)
        source_org, target_org = await self._organizations(
            request, user=user, requester_role=requester_role
        )
        source_fingerprint, target_fingerprint = await self._fingerprints(request)
        report = await self._preflight(
            request,
            source_fingerprint=source_fingerprint,
            target_fingerprint=target_fingerprint,
            user=user,
            organization_id=source_org,
        )
        merged = request.model_dump(mode="json")
        merged["source"]["cluster_fingerprint"] = source_fingerprint
        merged["target"]["cluster_fingerprint"] = target_fingerprint
        source_name = request.source.resource_name
        target_name = request.target.resource_name or source_name
        return await repository.replace_plan(
            self.db,
            row,
            expected_revision=expected_revision,
            values={
                "status": "ready" if not report["blockers"] else "blocked",
                **_authority_values(authority),
                "source_organization_id": source_org,
                "target_organization_id": target_org,
                "source_cluster_id": request.source.cluster_id,
                "source_project": request.source.project,
                "source_resource_name": source_name,
                "source_member": request.source.member,
                "source_cluster_fingerprint": source_fingerprint,
                "target_cluster_id": request.target.cluster_id,
                "target_project": request.target.project,
                "target_resource_name": target_name,
                "target_member": request.target.member,
                "target_cluster_fingerprint": target_fingerprint,
                "source_key": canonical_resource_key(
                    _enum_value(request.scope),
                    request.source.cluster_id,
                    request.source.project,
                    source_name,
                ),
                "target_key": canonical_resource_key(
                    _enum_value(request.scope),
                    request.target.cluster_id,
                    request.target.project,
                    target_name,
                ),
                "payload_hash": canonical_json_hash(merged),
                "request_payload": sanitize_payload(merged),
                "preflight_payload": report,
                "retention_hours": request.retention_hours,
                "checkpoint_requested": request.checkpoint_requested,
                "external_cutover_required": request.external_cutover_required,
            },
        )

    async def _execute_plan_with_digest(
        self,
        plan_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        digest: str,
        reason: str,
        acknowledgements: list[str],
        user: Any,
        authority: Any,
    ) -> tuple[MigrationJob, bool]:
        requester = authority.pin.identity.email
        requester_role = authority.role.value
        replay = await repository.get_job_by_request(
            self.db, requester, idempotency_key
        )
        if replay is not None:
            from .context import _authoritative_clients, _job_plan

            _require_plan_actor(replay, authority)
            replay_plan = await _job_plan(replay)
            _require_plan_actor(replay_plan, authority)
            if replay.payload_hash != digest:
                raise MigrationConflict(
                    "This Idempotency-Key was already used for another request."
                )
            # A retry is still an authorization decision. Rebuild both endpoint ownership,
            # current Authentik standing, registration fingerprints, and the immutable plan pin
            # before disclosing the existing job to an actor whose role may have changed.
            await _authoritative_clients(replay, current_authority=authority)
            return replay, False
        plan = await repository.get_plan(self.db, plan_id, lock=True)
        if plan is None:
            raise MigrationNotFound("Migration plan not found.")
        _require_plan_actor(plan, authority)
        if plan.revision != expected_revision:
            raise MigrationConflict("The plan changed. Reload it before execution.")
        if plan.expires_at <= repository.utcnow():
            plan.status = "expired"
            await self.db.commit()
            raise MigrationConflict("The plan expired. Run preflight again.")
        if plan.status not in {"ready", "blocked"}:
            raise MigrationConflict("The plan cannot be executed in its current state.")
        request = MigrationPlanCreateRequest.model_validate(plan.request_payload)
        # The final preflight and durable hierarchy-lock insertion must share the same cluster
        # serialization boundary as ordinary creates. Otherwise a create can pass its last
        # fence check after this preflight but before the migration locks are committed.
        async with _serialize_migration_clusters(
            self.db,
            (request.source.cluster_id, request.target.cluster_id),
        ):
            source_org, _ = await self._organizations(
                request, user=user, requester_role=requester_role
            )
            source_fingerprint, target_fingerprint = await self._fingerprints(request)
            if (
                source_fingerprint != plan.source_cluster_fingerprint
                or target_fingerprint != plan.target_cluster_fingerprint
            ) and not await self._renewed_since(request, plan):
                raise MigrationConflict("A cluster registration changed. Create a new plan.")
            report = await self._preflight(
                request,
                source_fingerprint=source_fingerprint,
                target_fingerprint=target_fingerprint,
                user=user,
                organization_id=source_org,
            )
            self._require_execution_stage(request, report)
            approved_report = dict(plan.preflight_payload or {})
            if approval_report_hash(report) != approval_report_hash(approved_report):
                revised = await repository.revise_plan_preflight(
                    self.db,
                    plan,
                    report=report,
                )
                raise MigrationConflict(
                    "Fresh preflight changed approval-relevant migration evidence. "
                    f"Reload and approve plan revision {revised.revision}."
                )
            validate_acknowledgements(
                intent=_enum_value(request.intent),
                strategy=str(report.get("strategy") or ""),
                external_cutover_required=request.external_cutover_required,
                acknowledgements=acknowledgements,
                source_running=report_has_running_source(report),
                unmeasured_size=report_needs_unmeasured_size_consent(report),
            )
            source_client, _target_client = await self._clients(
                request,
                source_fingerprint=source_fingerprint,
                target_fingerprint=target_fingerprint,
            )
            await _blocking(
                enforce_migration_billing_policy,
                source_client,
                project=request.source.project,
                report=report,
                reason=reason,
                acknowledgements=acknowledgements,
                label="migration.billing_policy",
            )
            if report["blockers"]:
                raise MigrationConflict("Fresh preflight found blockers. Reload the plan.")
            try:
                row, created = await repository.create_job(
                    self.db,
                    plan=plan,
                    requester=requester,
                    requester_role=requester_role,
                    actor_issuer=authority.pin.identity.issuer,
                    actor_subject=authority.pin.identity.subject,
                    actor_email=authority.pin.identity.email,
                    actor_provider_uuid=authority.pin.provider_uuid,
                    idempotency_key=idempotency_key,
                    payload_hash=digest,
                    reason=reason,
                    acknowledgements=acknowledgements,
                )
            except repository.RepositoryConflict as exc:
                raise MigrationConflict(str(exc)) from exc
        if created:
            _schedule(run_migration_job(row.job_id, allow_mutation=True))
        return row, created

    async def execute_plan(
        self,
        plan_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        reason: str,
        acknowledgements: list[str],
        user: Any,
        requester_role: str,
    ) -> tuple[MigrationJob, bool]:
        del requester_role
        authority = await AuthentikAuthorityVerifier(self.settings).pin_user(user)
        digest = canonical_json_hash(
            {
                "plan_id": plan_id,
                "revision": expected_revision,
                "reason": reason,
                "acknowledgements": acknowledgements,
            }
        )
        return await self._execute_plan_with_digest(
            plan_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            digest=digest,
            reason=reason,
            acknowledgements=acknowledgements,
            user=user,
            authority=authority,
        )

    async def execute_compatibility_copy(
        self,
        request: MigrationPlanCreateRequest,
        *,
        idempotency_key: str,
        reason: str,
        acknowledgements: list[str],
        user: Any,
        requester_role: str,
    ) -> tuple[MigrationJob, bool]:
        """Accept a legacy Copy Instance request with retry-stable job identity.

        The digest deliberately excludes a generated plan id. A retried legacy request can find
        its existing job before another short-lived plan is created.
        """
        if _enum_value(request.scope) != "instance" or _enum_value(request.intent) != "copy":
            raise MigrationConflict("The compatibility adapter accepts instance copy only.")
        authority = await AuthentikAuthorityVerifier(self.settings).pin_user(user)
        normalized_acknowledgements = sorted(set(acknowledgements))
        digest = canonical_json_hash(
            {
                "contract": "legacy-instance-copy-v1",
                "request": request.model_dump(mode="json"),
                "reason": reason,
                "acknowledgements": normalized_acknowledgements,
            }
        )
        replay = await repository.get_job_by_request(
            self.db, authority.pin.identity.email, idempotency_key
        )
        if replay is not None:
            from .context import _authoritative_clients, _job_plan

            _require_plan_actor(replay, authority)
            replay_plan = await _job_plan(replay)
            _require_plan_actor(replay_plan, authority)
            if replay.payload_hash != digest:
                raise MigrationConflict(
                    "This Idempotency-Key was already used for another request."
                )
            await _authoritative_clients(replay, current_authority=authority)
            return replay, False

        plan = await self.create_plan(
            request,
            user=user,
            requester_role=requester_role,
        )
        return await self._execute_plan_with_digest(
            plan.plan_id,
            expected_revision=plan.revision,
            idempotency_key=idempotency_key,
            digest=digest,
            reason=reason,
            acknowledgements=normalized_acknowledgements,
            user=user,
            authority=authority,
        )

    async def get_job(self, job_id: str) -> MigrationJob:
        row = await repository.get_job(self.db, job_id)
        if row is None:
            raise MigrationNotFound("Migration job not found.")
        return row

    async def list_jobs(
        self,
        *,
        visibility: repository.MigrationListVisibility,
        limit: int,
        offset: int,
    ) -> tuple[list[MigrationJob], int]:
        return await repository.list_jobs(
            self.db,
            visibility=visibility,
            limit=limit,
            offset=offset,
        )
