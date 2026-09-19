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
from typing import Annotated, Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

from app.core.blocking import run_blocking
from app.core.cache import get_cache_manager
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
from app.core.snapshot_billing_gate import audit_billing_override
from app.services.lxd import mobility
from app.services.lxd import snapshots as snapshots_service
from app.services.lxd.availability import require_client
from app.services.lxd.lazy_client import client_for
from app.services.lxd.proxy import PROXY_MAX_IN_FLIGHT, proxy_executor
from app.services.lxd_service import LXDService, get_lxd_service
from app.services.orcahub_webhook import schedule_orcahub_event

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

router = APIRouter(
    prefix="/instances",
    tags=["Instances - Mobility"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)

_LXD_CALL_TIMEOUT = 60


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
    target_name: str = Field(..., min_length=1, max_length=63, description="Name for the copy on the destination")
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
    transport: str = Field(..., description="Transport actually used: copy, migration or stream")
    operation_id: Optional[str] = Field(None, description="LXD operation to poll on the destination")
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


async def _settle_copy(client, operation_url: Optional[str], project: str) -> Dict[str, Any]:
    """Wait briefly on the copy operation so a 202 is not the last thing anyone hears.

    important: a cross-cluster migration is accepted by the target before any bytes move, so the
    202 says only that the request was understood. Every way the transfer can fail afterwards
    (the target cannot open the source, the pool fills, the secrets are refused) lands in the
    operation and nowhere else. Without this the copy is reported as started and its failure is
    never surfaced anywhere an operator looks.

    A large instance legitimately outlasts the window, which is reported as unsettled rather than
    as either outcome, the same contract the snapshot surface uses.
    """
    operation_id = (operation_url or "").rsplit("/", 1)[-1]
    if not operation_id:
        return {"status": None, "err": None, "settled": True}
    try:
        return await _blocking(
            snapshots_service.wait_for_operation,
            client,
            operation_id,
            project,
            label="mobility.wait",
        )
    except Exception as exc:  # noqa: BLE001 - a failed wait is not a failed operation
        logger.debug("Could not wait on copy operation %s: %s", operation_id, exc)
        return {"status": None, "err": None, "settled": False}


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
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(require_partner()),
):
    # Both ends are authorized. Checking only the source would let a partner read out of a
    # cluster they own and write into one they do not, which is the whole boundary.
    source_project = verify_project_access(body.host, body.project)
    target_project = verify_project_access(body.target_host, body.target_project)
    body = body.model_copy(
        update={"project": source_project, "target_project": target_project}
    )

    source = await _client(lxd_service, body.host)
    target = await _client(lxd_service, body.target_host)

    # A copy reads the source and duplicates it. That does not move the storage the external
    # schedule is measured on, but the rule an operator carries is "anything on a billing-linked
    # instance is acknowledged", and a rule with an exception is one nobody remembers the shape of.
    policy = await _read_source_policy(source, instance_name, body.project)
    justification = _gate_copy(policy, instance_name, body)

    transport = mobility.choose_transport(body.host, body.target_host)

    try:
        if transport == mobility.TRANSPORT_COPY:
            operation = await _blocking(
                mobility.copy_within_cluster,
                source,
                label="mobility.copy",
                source_name=instance_name,
                source_project=body.project,
                target_name=body.target_name,
                target_project=body.target_project,
                target_member=body.target_member,
                target_pool=body.target_pool,
            )
            # A same-cluster copy is one LXD call, so the operation lives on the source server.
            operation_client = source
        else:
            operation = await _cross_cluster(source, target, instance_name, body)
            # A pull migration is driven by the destination, so its operation is the target's.
            operation_client = target
    except HTTPException:
        raise
    except snapshots_service.LXDOperationUnreadable as exc:
        # Accepted with nothing to wait on. Upstream problem, and a copy reported as started when
        # its completion cannot be confirmed is the defect this path was rewritten to remove.
        _audit_move(instance_name, body, transport, result="failure", error=str(exc))
        raise HTTPException(status_code=502, detail=str(exc))
    except mobility.MobilityError as exc:
        _audit_move(instance_name, body, transport, result="failure", error=str(exc))
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        _audit_move(instance_name, body, transport, result="failure", error=str(exc))
        raise lxd_http_error(exc, "Instance", instance_name)

    operation_id = (operation or "").rsplit("/", 1)[-1] or None
    outcome = await _settle_copy(operation_client, operation, body.target_project)

    if outcome.get("failed"):
        detail = outcome.get("err") or "unknown error"
        if mobility.is_unreachable_error(Exception(detail)):
            # The one failure an operator cannot diagnose from the raw message, because the link
            # that broke is the one this control plane never uses.
            detail = (
                f"{detail}. A cross-cluster copy is pulled by the destination, so "
                f"{body.target_host} has to open a connection to {body.host} directly. It could not."
            )
        _audit_move(
            instance_name, body, transport, result="failure", error=detail,
            operation_id=operation_id,
        )
        raise HTTPException(
            status_code=502, detail=f"The copy failed after it was accepted: {detail}"
        )

    _audit_move(
        instance_name, body, transport,
        result="success" if outcome.get("settled") else "accepted",
        operation_id=operation_id,
    )
    if justification:
        audit_billing_override(
            "copy", instance_name, body.host, body.project, policy, justification,
            user.email or user.user_id,
        )
    # The reader's cache is invalidated either way: something is happening on the destination and
    # a stale list is wrong in both cases.
    get_cache_manager().invalidate_instance_cache()
    if outcome.get("settled"):
        # important: announced only once the operation settled. `instance.created` tells the
        # upstream product a resource EXISTS, and it meters what it is told about. Emitting on the
        # unsettled branch announced an instance whose transfer had been accepted and might still
        # fail, which is the same "reported before it happened" defect the settle check was added
        # to remove, one step further downstream. An unsettled copy that does complete is picked
        # up by that product's own inventory sweep, so the cost of waiting is a delay rather than
        # a miss, while the cost of announcing early is a resource in a ledger that never appeared.
        schedule_orcahub_event(
            event_type="instance.created",
            cluster_id=body.target_host,
            project=body.target_project,
            resource_type="INSTANCE",
            name=body.target_name,
        )
    return MoveResponse(
        success=True,
        instance_name=instance_name,
        target_host=body.target_host,
        target_project=body.target_project,
        target_name=body.target_name,
        transport=transport,
        operation_id=operation_id,
        message=(
            (
                f"{instance_name} copied to {body.target_host}/{body.target_project} "
                f"as {body.target_name}."
            )
            if outcome.get("settled")
            else (
                f"Copying {instance_name} to {body.target_host}/{body.target_project} "
                f"as {body.target_name} using the {transport} transport."
            )
        ),
    )


async def _cross_cluster(source, target, instance_name: str, body: MoveRequest):
    """Run the cross-cluster copy over LXD's native pull migration.

    The source stands up a migration and hands back one-time secrets; the target pulls with them.
    That means the TARGET opens the connection to the SOURCE, a link this control plane never
    uses itself and therefore cannot test in advance: LXD exposes no call that answers "can you
    reach this host". A failure on that link surfaces in the target's operation, which is why the
    caller polls it rather than trusting the 202.

    A streaming transport that would carry the bytes through this control plane instead exists in
    the service layer and is deliberately not wired up here. It has never executed, and making it
    production-grade needs a queued job with its own worker pool and bounded disk rather than an
    hour-long hold on the pool that also serves console and terminal traffic.
    """
    certificate = await _blocking(
        mobility.server_certificate, source, label="mobility.cert"
    )
    definition = await _blocking(
        mobility.read_instance_definition,
        source,
        instance_name,
        body.project,
        label="mobility.definition",
    )
    operation_url, secrets_map = await _blocking(
        mobility.begin_source_migration,
        source,
        label="mobility.begin",
        instance_name=instance_name,
        project=body.project,
    )
    try:
        return await _blocking(
            mobility.accept_migration_on_target,
            target,
            label="mobility.accept",
            target_name=body.target_name,
            target_project=body.target_project,
            source_operation_url=operation_url,
            source_certificate=certificate,
            secrets=secrets_map,
            target_member=body.target_member,
            target_pool=body.target_pool,
            definition=definition,
        )
    except Exception as exc:  # noqa: BLE001
        if mobility.is_unreachable_error(exc):
            raise mobility.MobilityError(
                f"{body.target_host} could not reach {body.host} to pull {instance_name}. "
                "A cross-cluster copy is pulled by the destination, so the two clusters need a "
                "direct network path between them."
            ) from exc
        raise


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
