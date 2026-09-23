"""Moving a workload between projects, cluster members and clusters (dashboard, JWT).

Admin and Partner only. This is a platform capability, not a tenant product: it is how an
operator decommissions a node, rebalances capacity, or promotes a workload out of a scratch
project. OrcaHub sells data protection; this moves running workloads, and the two are different
jobs even though both touch the same instances.

Copy, never move. The source instance survives every path through this router, so a destination
that turns out wrong is recoverable and the operator decides when to remove the original.

Transport is chosen by the server, not the caller, because the caller cannot know which one a
given pair of endpoints supports: `copy` inside one cluster, native pull `migration` between two.

A cross-cluster migration requires the TARGET to open a connection to the SOURCE. LXD offers no
call that answers "can you reach this host", so that is not probed in advance: it is learned from
the attempt, which is why the operation is polled rather than assumed. A streaming transport
through the control plane is drafted in the service layer and deliberately not reachable from
here; see documentation/instances/operator-snapshots-and-mobility.md for why.
"""
import logging
import re
from typing import Annotated, Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.blocking import run_blocking
from app.core.config import get_settings
from app.core.database import get_db
from app.core.dependencies import (
    AuthenticatedUser,
    check_read_only_mode,
    require_partner,
    require_tenant,
)
from app.core.lxd_names import LXD_NAME_DESCRIPTION, LXD_NAME_PATTERN
from app.core.lxd_errors import lxd_http_error
from app.core.org_scope import (
    org_scope_dependency,
    project_is_visible,
    verify_cluster_access,
    verify_project_access,
)
from app.services.activity_logger import get_activity_logger
from app.services.lxd import mobility
from app.services.lxd import snapshots as snapshots_service
from app.services.lxd.availability import require_client
from app.services.lxd.lazy_client import client_for
from app.services.lxd.proxy import PROXY_MAX_IN_FLIGHT, proxy_executor
from app.services.lxd_service import LXDService, get_lxd_service
from app.models.migration_schemas import (
    MigrationEndpointRef,
    MigrationIntent,
    MigrationMappings,
    MigrationPlanCreateRequest,
    MigrationScope,
)
from app.services.migrations import MigrationService
from app.services.migrations.authority import (
    AuthorityDeniedError,
    AuthorityIdentityError,
    CurrentAuthorityError,
)
from app.services.migrations.orchestrator import (
    MigrationAuthorityError,
    MigrationConflict,
)
from app.services.migrations.security import MigrationSecurityError, sanitize_error

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

router = APIRouter(
    prefix="/instances",
    tags=["Instances - Mobility"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)

_LXD_CALL_TIMEOUT = 60
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _blocking(fn, *args, label: str, timeout: int = _LXD_CALL_TIMEOUT, **kwargs):
    return run_blocking(
        fn,
        *args,
        timeout=timeout,
        label=label,
        executor=proxy_executor,
        max_in_flight=PROXY_MAX_IN_FLIGHT,
        **kwargs,
    )


class MovePreflight(BaseModel):
    instance_name: str = Field(..., description="The instance that would be copied")
    source_host: str = Field(..., description="Cluster the instance lives on")
    source_project: str = Field(..., description="LXD project the instance lives in")
    footprint_bytes: Optional[int] = Field(None, description="Root disk usage, when the driver reports it")
    architecture: Optional[str] = Field(None, description="The instance's architecture, which a destination must be able to run")
    destinations: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Clusters the caller may copy into, each with its projects, pools and headroom",
    )
    blockers: List[str] = Field(default_factory=list, description="Reasons a copy cannot proceed")


class MoveRequest(BaseModel):
    host: str = Field(..., description="Cluster the source instance lives on")
    project: str = Field("default", description="LXD project the source instance lives in")
    target_host: str = Field(..., description="Cluster to copy into")
    target_project: str = Field("default", description="LXD project to copy into")
    target_name: str = Field(
        ...,
        min_length=1,
        max_length=63,
        pattern=LXD_NAME_PATTERN,
        description="Name for the copy on the destination",
    )
    target_member: Optional[str] = Field(None, description="Cluster member to place the copy on, for a clustered host")
    target_pool: Optional[str] = Field(None, description="Storage pool for the copy's root disk")
    acknowledge_billing_impact: bool = Field(
        False,
        description="Proceed when the source's snapshots are billed elsewhere. Requires a reason",
    )
    reason: Optional[str] = Field(
        None, max_length=280, description="Why the copy is being made. Required on a billed source"
    )


class MoveResponse(BaseModel):
    success: bool = Field(..., description="Whether the copy was accepted")
    instance_name: str = Field(..., description="The source instance")
    target_host: str = Field(..., description="Cluster the copy is landing on")
    target_project: str = Field(..., description="Project the copy is landing in")
    target_name: str = Field(..., description="Name of the copy")
    transport: str = Field(
        ...,
        description="Transport actually used: durable, copy, migration or stream",
    )
    operation_id: Optional[str] = Field(None, description="LXD operation to poll on the destination")
    migration_job_id: Optional[str] = Field(
        None, description="Durable Migration Center job, when that engine accepted the copy"
    )
    message: str = Field(..., description="Operator-facing summary")


async def _client(lxd_service: LXDService, host: str):
    return require_client(await client_for(lxd_service, host, label="mobility"), host)


async def _read_source_policy(source, instance_name: str, project: str) -> Dict[str, Any]:
    try:
        return await _blocking(
            snapshots_service.read_instance_policy,
            source,
            instance_name,
            project,
            label="mobility.policy",
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise lxd_http_error(exc, "Instance", instance_name)


def _gate_copy(policy: Dict[str, Any], instance_name: str, body: "MoveRequest") -> Optional[str]:
    """Apply the billing gate outside the handler's try, and record a refusal."""
    from app.core.snapshot_billing_gate import enforce_billing_impact

    try:
        return enforce_billing_impact(
            policy,
            instance_name,
            acknowledged=body.acknowledge_billing_impact,
            reason=body.reason,
        )
    except HTTPException:
        _audit_move(instance_name, body, "none", result="denied",
                    error="billing_impact_acknowledgement_required")
        raise


def _validated_idempotency_key(value: Optional[str]) -> str:
    if value is None:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="Idempotency-Key is required while durable copy is enabled.",
        )
    if not _IDEMPOTENCY_KEY.fullmatch(value):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key contains unsupported characters.",
        )
    return value


async def _durable_copy(
    *,
    db: AsyncSession,
    lxd_service: LXDService,
    user: AuthenticatedUser,
    instance_name: str,
    body: MoveRequest,
    idempotency_key: Optional[str],
    justification: Optional[str],
) -> MoveResponse:
    reason = (body.reason or "").strip()
    if len(reason) < 8:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A reason of at least 8 characters is required for a durable copy.",
        )
    settings = get_settings()
    request = MigrationPlanCreateRequest(
        scope=MigrationScope.INSTANCE,
        intent=MigrationIntent.COPY,
        source=MigrationEndpointRef(
            cluster_id=body.host,
            project=body.project,
            resource_name=instance_name,
        ),
        target=MigrationEndpointRef(
            cluster_id=body.target_host,
            project=body.target_project,
            resource_name=body.target_name,
            member=body.target_member,
        ),
        retention_hours=settings.migration_default_retention_hours,
        checkpoint_requested=False,
        external_cutover_required=False,
        mappings=MigrationMappings(
            storage_pools={"root": body.target_pool} if body.target_pool else {}
        ),
    )
    acknowledgements = ["copy-impact-reviewed", "billing-impact-reviewed"]
    if justification:
        acknowledgements.append("billing-impact-acknowledged")
    try:
        row, _created = await MigrationService(db, lxd_service).execute_compatibility_copy(
            request,
            idempotency_key=_validated_idempotency_key(idempotency_key),
            reason=reason,
            acknowledgements=acknowledgements,
            user=user,
            requester_role="partner",
        )
    except (MigrationAuthorityError, AuthorityDeniedError, AuthorityIdentityError) as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except CurrentAuthorityError as exc:
        logger.warning("Durable copy authority verification failed: %s", exc.code)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Current migration authority could not be verified.",
        ) from exc
    except (MigrationConflict, MigrationSecurityError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("Durable copy request failed: %s", sanitize_error(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Migration service is temporarily unavailable.",
        ) from exc

    return MoveResponse(
        success=True,
        instance_name=instance_name,
        target_host=body.target_host,
        target_project=body.target_project,
        target_name=body.target_name,
        transport="durable",
        operation_id=None,
        migration_job_id=row.job_id,
        message=(
            f"Copy job for {instance_name} was accepted by Migration Center. "
            "Track validation and transfer progress there."
        ),
    )


@router.get("/{instance_name}/move-preflight", response_model=MovePreflight)
async def move_preflight(
    instance_name: Annotated[
        str,
        Path(pattern=LXD_NAME_PATTERN, description=f"Instance to act on. {LXD_NAME_DESCRIPTION}"),
    ],
    host: str = Query(..., description="Cluster the instance lives on"),
    project: str = Query("default", description="LXD project the instance lives in"),
    lxd_service: LXDService = Depends(get_lxd_service),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    project = verify_project_access(host, project)

    source = await _client(lxd_service, host)
    blockers: List[str] = []
    try:
        footprint = await _blocking(
            mobility.instance_footprint, source, instance_name, project,
            label="mobility.footprint",
        )
        source_arch = await _blocking(
            mobility.instance_architecture, source, instance_name, project,
            label="mobility.arch",
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise lxd_http_error(exc, "Instance", instance_name)

    # Only clusters this caller may already reach are offered. Enumerating a destination the
    # caller cannot use would leak the fleet's shape to a partner scoped to one cluster.
    destinations: List[Dict[str, Any]] = []
    for candidate in lxd_service.get_node_names() or []:
        try:
            verify_cluster_access(candidate)
        except HTTPException:
            continue
        entry: Dict[str, Any] = {"host": candidate, "same_cluster": candidate == host}
        try:
            client = await client_for(lxd_service, candidate, label="mobility.preflight")
            if client is None:
                entry["reachable"] = False
                destinations.append(entry)
                continue
            entry["reachable"] = True
            # An architecture mismatch is a hard refusal from LXD, not a slow path, so the
            # destination is offered as unavailable with the reason rather than allowed to fail
            # after the operator has committed to it.
            arches = await _blocking(
                mobility.host_architectures, client, label="mobility.arches"
            )
            entry["architectures"] = arches
            if not mobility.architecture_compatible(source_arch, arches):
                entry["compatible"] = False
                entry["reason"] = (
                    f"Runs {', '.join(arches) or 'an unknown architecture'}, and this instance "
                    f"is {source_arch}."
                )
                destinations.append(entry)
                continue
            entry["compatible"] = True
            projects = await _blocking(
                lambda c: [
                    p.rsplit("/", 1)[-1]
                    for p in (c.api.projects.get().json().get("metadata") or [])
                ],
                client,
                label="mobility.projects",
            )
            projects = [
                candidate_project
                for candidate_project in projects
                if project_is_visible(candidate, candidate_project)
            ]
            pools = await _blocking(
                lambda c: [
                    p.rsplit("/", 1)[-1]
                    for p in (c.api.storage_pools.get().json().get("metadata") or [])
                ],
                client,
                label="mobility.pools",
            )
            entry["projects"] = projects
            entry["pools"] = await _blocking(
                _pools_with_headroom, client, pools, label="mobility.headroom"
            )
        except Exception as exc:  # noqa: BLE001 - one unreachable peer must not fail the read
            logger.debug("Preflight skipped %s: %s", candidate, exc)
            entry["reachable"] = False
        destinations.append(entry)

    usable = [d for d in destinations if d.get("reachable") and d.get("compatible")]
    if not any(d.get("reachable") for d in destinations):
        blockers.append("No destination cluster is currently reachable.")
    elif not usable:
        blockers.append(
            f"No reachable cluster can run a {source_arch or 'this'} instance."
        )

    return MovePreflight(
        instance_name=instance_name,
        source_host=host,
        source_project=project,
        footprint_bytes=footprint,
        architecture=source_arch,
        destinations=destinations,
        blockers=blockers,
    )


def _pools_with_headroom(client, pools: List[str]) -> List[Dict[str, Any]]:
    """Each pool with its free space, read on a worker thread rather than the event loop."""
    return [{"name": pool, "headroom": mobility.pool_headroom(client, pool)} for pool in pools]


@router.post("/{instance_name}/copy", response_model=MoveResponse, status_code=202)
async def copy_instance(
    instance_name: Annotated[
        str,
        Path(pattern=LXD_NAME_PATTERN, description=f"Instance to act on. {LXD_NAME_DESCRIPTION}"),
    ],
    body: MoveRequest,
    lxd_service: LXDService = Depends(get_lxd_service),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(require_partner()),
) -> MoveResponse:
    # Both ends are authorized. Checking only the source would let a partner read out of a
    # cluster they own and write into one they do not, which is the whole boundary.
    source_project = verify_project_access(body.host, body.project)
    target_project = verify_project_access(body.target_host, body.target_project)
    body = body.model_copy(
        update={"project": source_project, "target_project": target_project}
    )

    settings = get_settings()
    if not (
        settings.migration_center_enabled
        and settings.migration_durable_copy_enabled
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "durable_copy_admission_disabled",
                "message": "Durable Copy Instance admission is not enabled.",
                "remediation": (
                    "Use Migration Center preflight and enable the reviewed durable-copy "
                    "rollout stage before submitting a copy."
                ),
            },
        )

    source = await _client(lxd_service, body.host)

    # A copy reads the source and duplicates it. That does not move the storage the external
    # schedule is measured on, but the rule an operator carries is "anything on a billing-linked
    # instance is acknowledged", and a rule with an exception is one nobody remembers the shape of.
    policy = await _read_source_policy(source, instance_name, body.project)
    justification = _gate_copy(policy, instance_name, body)

    return await _durable_copy(
        db=db,
        lxd_service=lxd_service,
        user=user,
        instance_name=instance_name,
        body=body,
        idempotency_key=idempotency_key,
        justification=justification,
    )


def _audit_move(
    instance_name: str,
    body: MoveRequest,
    transport: str,
    *,
    result: str,
    error: Optional[str] = None,
    operation_id: Optional[str] = None,
) -> None:
    activity_logger.log_activity(
        action="instance.migrate",
        target_type="instance",
        target_id=instance_name,
        target_host=body.host,
        target_project=body.project,
        result=result,
        error_message=error,
        details={
            "target_host": body.target_host,
            "target_project": body.target_project,
            "target_name": body.target_name,
            "target_member": body.target_member,
            "target_pool": body.target_pool,
            "transport": transport,
            "operation_id": operation_id,
        },
    )
