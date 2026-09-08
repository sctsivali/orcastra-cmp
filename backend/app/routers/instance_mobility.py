"""Moving a workload between projects, cluster members and clusters (dashboard, JWT).

Admin and Partner only. This is a platform capability, not a tenant product: it is how an
operator decommissions a node, rebalances capacity, or promotes a workload out of a scratch
project. OrcaHub sells data protection; this moves running workloads, and the two are different
jobs even though both touch the same instances.

Copy, never move. The source instance survives every path through this router, so a destination
that turns out wrong is recoverable and the operator decides when to remove the original.

Transport is chosen by the server, not the caller, because the caller cannot know which one a
given pair of endpoints supports. `force_transport` exists only so the streaming fallback can be
exercised deliberately in QA: a path that only runs on an unusual network topology would
otherwise ship having never executed, which is this codebase's most expensive recurring defect.
"""
import logging
import secrets
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.blocking import run_blocking
from app.core.cache import get_cache_manager
from app.core.dependencies import (
    AuthenticatedUser,
    check_read_only_mode,
    require_partner,
    require_tenant,
)
from app.core.lxd_errors import lxd_http_error
from app.core.org_scope import org_scope_dependency, verify_cluster_access
from app.services.activity_logger import get_activity_logger
from app.services.lxd import mobility
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
# The transfer artifact is named so it can never collide with a backup someone else made, and so
# an abandoned one is identifiable as ours during a sweep.
_TRANSFER_BACKUP_PREFIX = "ops-xfer-"


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
    force_transport: Optional[str] = Field(
        None,
        description="QA only. Force 'migration' or 'stream' instead of letting the server choose",
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


@router.get("/{instance_name}/move-preflight", response_model=MovePreflight)
async def move_preflight(
    instance_name: str,
    host: str = Query(..., description="Cluster the instance lives on"),
    project: str = Query("default", description="LXD project the instance lives in"),
    lxd_service: LXDService = Depends(get_lxd_service),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    verify_cluster_access(host)

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
    instance_name: str,
    body: MoveRequest,
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(require_partner()),
):
    # Both ends are authorized. Checking only the source would let a partner read out of a
    # cluster they own and write into one they do not, which is the whole boundary.
    verify_cluster_access(body.host)
    verify_cluster_access(body.target_host)

    if body.force_transport and body.force_transport not in (
        mobility.TRANSPORT_MIGRATION,
        mobility.TRANSPORT_STREAM,
    ):
        raise HTTPException(
            status_code=422,
            detail="force_transport must be 'migration' or 'stream' when supplied.",
        )

    source = await _client(lxd_service, body.host)
    target = await _client(lxd_service, body.target_host)
    transport = body.force_transport or mobility.choose_transport(body.host, body.target_host)

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
        else:
            operation, transport = await _cross_cluster(
                source, target, instance_name, body, transport
            )
    except HTTPException:
        raise
    except mobility.MobilityError as exc:
        _audit_move(instance_name, body, transport, result="failure", error=str(exc))
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        _audit_move(instance_name, body, transport, result="failure", error=str(exc))
        raise lxd_http_error(exc, "Instance", instance_name)

    operation_id = (operation or "").rsplit("/", 1)[-1] or None
    _audit_move(instance_name, body, transport, result="accepted", operation_id=operation_id)
    # A new instance exists on the destination, so both the reader's cache and OrcaHub's ledger
    # are now behind. Without the event the copy is invisible to OrcaHub until its next sweep.
    get_cache_manager().invalidate_instance_cache()
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
            f"Copying {instance_name} to {body.target_host}/{body.target_project} "
            f"as {body.target_name} using the {transport} transport."
        ),
    )


async def _cross_cluster(source, target, instance_name: str, body: MoveRequest, transport: str):
    """Run the cross-cluster copy, preferring native migration.

    Migration is attempted first and the streaming fallback is used only when the failure says
    the target could not open the source. Any other failure is raised as itself: silently
    downgrading a permissions or state error to the slow path would fail the same way, minutes
    later, having consumed pool space on the source in the meantime.
    """
    if transport == mobility.TRANSPORT_MIGRATION:
        try:
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
            operation = await _blocking(
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
            return operation, mobility.TRANSPORT_MIGRATION
        except Exception as exc:  # noqa: BLE001
            if not mobility.is_unreachable_error(exc):
                raise
            logger.warning(
                "Native migration unavailable between %s and %s (%s); using the stream transport",
                body.host, body.target_host, exc,
            )

    return await _stream_copy(source, target, instance_name, body), mobility.TRANSPORT_STREAM


async def _stream_copy(source, target, instance_name: str, body: MoveRequest):
    """Export on the source, feed the bytes to the target's import, always clean up.

    The backup object is a real artifact on someone's storage pool. It is deleted on every path
    out of this function, including the failure ones, because an abandoned export is invisible
    in the UI and silently consumes the pool until someone notices it is full.
    """
    footprint = await _blocking(
        mobility.instance_footprint, source, instance_name, body.project,
        label="mobility.footprint",
    )
    if footprint and footprint > mobility.STREAM_MAX_BYTES:
        raise mobility.MobilityError(
            f"{instance_name} uses {footprint // 10**9} GB, above the "
            f"{mobility.STREAM_MAX_BYTES // 10**9} GB ceiling for a streamed copy. "
            "Give the destination cluster a network path to the source and retry, which uses "
            "the direct transport and does not pass through this control plane."
        )

    backup_name = f"{_TRANSFER_BACKUP_PREFIX}{secrets.token_hex(6)}"
    try:
        await _blocking(
            mobility.create_backup,
            source,
            label="mobility.backup",
            timeout=900,
            instance_name=instance_name,
            project=body.project,
            backup_name=backup_name,
        )
        return await _blocking(
            _pipe_backup_to_target,
            source,
            target,
            instance_name,
            body.project,
            backup_name,
            body.target_name,
            body.target_project,
            body.target_pool,
            label="mobility.stream",
            timeout=mobility.TRANSFER_READ_TIMEOUT,
        )
    finally:
        await _blocking(
            mobility.delete_backup,
            source,
            label="mobility.cleanup",
            instance_name=instance_name,
            project=body.project,
            backup_name=backup_name,
        )


def _pipe_backup_to_target(
    source,
    target,
    instance_name: str,
    project: str,
    backup_name: str,
    target_name: str,
    target_project: str,
    target_pool: Optional[str],
):
    """Stream the export straight into the import, never holding the whole tarball.

    important: the import POST goes through a session that does not retry. The pooled session
    retries POST on 5xx, and a retried import would create the instance twice, which for a
    multi-gigabyte transfer means two half-built instances and a full pool.
    """
    response = mobility.open_backup_stream(
        source, instance_name=instance_name, project=project, backup_name=backup_name
    )
    response.raise_for_status()

    headers = {
        "Content-Type": "application/octet-stream",
        "X-LXD-name": target_name,
    }
    if target_pool:
        headers["X-LXD-pool"] = target_pool

    session = mobility.non_retrying_session(target)
    url = f"{mobility.api_base(target)}/instances"
    result = session.post(
        url,
        params={"project": target_project or "default"},
        headers=headers,
        data=mobility.iter_chunks(response),
        timeout=(mobility.TRANSFER_CONNECT_TIMEOUT, mobility.TRANSFER_READ_TIMEOUT),
    )
    if result.status_code >= 400:
        raise mobility.MobilityError(
            f"The destination refused the import ({result.status_code}): {result.text[:300]}"
        )
    body = result.json() if result.content else {}
    return body.get("operation") if isinstance(body, dict) else None


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
