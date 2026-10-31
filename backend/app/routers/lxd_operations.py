"""
API Router for LXD Operations.
Provides endpoints for monitoring and managing LXD operations.
"""

import asyncio
import logging
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Callable, Dict, List, Optional, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from requests.exceptions import Timeout as RequestsTimeout
from sqlalchemy import or_, select

from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking
from app.core.database import async_session_maker
from app.core.dependencies import AuthenticatedUser, check_read_only_mode, require_partner
from app.core.error_sanitizer import sanitize_error
from app.core.lxd_path import safe_segment
from app.core.org_scope import (
    check_tenant_permission,
    org_scope_dependency,
    verify_cluster_access,
)
from app.core.rbac import Role, resolve_roles
from app.models.db_models import Organization, OrganizationMember
from app.services.instance_job_identity import resolve_instance_job_organization
from app.services.lxd.exceptions import LXDConnectionError
from app.services.lxd.lazy_client import client_for
# note: the allowlist moved beside the secret key set so every API-key surface reads this same
# function. The alias keeps this router's call site as it was.
from app.services.lxd.operation_metadata import (
    sanitize_operation_metadata as _sanitize_operation_metadata,
)
from app.services.migration_fence import (
    MigrationFenceConflict,
    assert_instance_not_migration_fenced,
    assert_operation_not_migration_owned,
    assert_project_not_migration_fenced,
)
from app.services.lxd_service import (
    LXDService,
    get_lxd_service,
    require_client,
)
from app.services.lxd.operation_ownership import (  # noqa: F401 - the router's names, kept for its tests
    INSTANCE_ROOTS as _INSTANCE_ROOTS,
    MAX_RESOURCE_URL_LENGTH as _MAX_RESOURCE_URL_LENGTH,
    PROJECT_RESOURCE_ROOTS as _PROJECT_RESOURCE_ROOTS,
    OperationOwnership as _OperationOwnership,
    operation_ownership as _operation_ownership,
    operation_value as _operation_value,
    resource_project as _resource_project,
)
from app.services.lxd.fanout import complete_reasons

logger = logging.getLogger(__name__)

# important: raised from require_tenant() to require_partner(). Every page that consumes this
# router declares useRequireRole(Role.PARTNER), so the UI already treated it as partner-tier while
# the API accepted a tenant. Mutations here were gated individually and reads were not, which is
# the pattern this closes. Org scope still applies on top, so this narrows the tier rather than
# replacing the boundary.
router = APIRouter(
    prefix="/operations",
    tags=["Operations"],
    dependencies=[Depends(require_partner()), Depends(org_scope_dependency)],
)

# Thread pool for parallel operations
_executor = ThreadPoolExecutor(max_workers=5)
_operation_executor = ThreadPoolExecutor(
    max_workers=8,
    thread_name_prefix="lxd_operation",
)
_OPERATION_MAX_IN_FLIGHT = 8
_OPERATION_MAX_IN_FLIGHT_PER_CLUSTER = 4
_OPERATION_SOCKET_TIMEOUT = (3, 8)
_OPERATION_LOOKUP_TIMEOUT_SECONDS = 12.0
_OPERATION_CANCEL_TIMEOUT_SECONDS = 12.0
_OPERATION_WAIT_GRACE_SECONDS = 10.0
_OperationResult = TypeVar("_OperationResult")

async def _operation_io(
    call: Callable[[], _OperationResult],
    *,
    host: str,
    label: str,
    timeout: float,
) -> _OperationResult:
    """Run one synchronous pylxd request with global and per-cluster bounds."""
    return await run_blocking(
        call,
        timeout=timeout,
        label=label,
        executor=_operation_executor,
        max_in_flight=_OPERATION_MAX_IN_FLIGHT,
        concurrency_key=host,
        max_in_flight_per_key=_OPERATION_MAX_IN_FLIGHT_PER_CLUSTER,
    )


def _response_operation(response: Any) -> Dict[str, Any]:
    """Extract one operation record without retaining the raw daemon response."""
    body = response.json()
    metadata = body.get("metadata") if isinstance(body, Mapping) else None
    if not isinstance(metadata, Mapping):
        raise ValueError("LXD returned an invalid operation response")
    return dict(metadata)


async def _lookup_operation(client: Any, operation_id: str, host: str) -> Dict[str, Any]:
    endpoint = client.api.operations[safe_segment(operation_id)]
    response = await _operation_io(
        partial(endpoint.get, timeout=_OPERATION_SOCKET_TIMEOUT),
        host=host,
        label=f"operations.lookup[{host}]",
        timeout=_OPERATION_LOOKUP_TIMEOUT_SECONDS,
    )
    return _response_operation(response)


async def _wait_for_operation(
    client: Any,
    operation_id: str,
    host: str,
    timeout: int,
) -> Dict[str, Any]:
    endpoint = client.api.operations[safe_segment(operation_id)].wait
    response = await _operation_io(
        partial(
            endpoint.get,
            params={"timeout": timeout},
            timeout=(_OPERATION_SOCKET_TIMEOUT[0], timeout + 5),
        ),
        host=host,
        label=f"operations.wait[{host}]",
        timeout=timeout + _OPERATION_WAIT_GRACE_SECONDS,
    )
    return _response_operation(response)


def _operation_not_found() -> HTTPException:
    # A missing operation, an ambiguous owner, and another organization's operation are
    # intentionally indistinguishable. An id must not be an ownership oracle.
    return HTTPException(status_code=404, detail="Operation not found")


async def _authoritative_project_access(
    *, host: str, project: str, actor: AuthenticatedUser
) -> bool:
    """Require one exact project owner and the actor's affiliation with that owner."""
    async with async_session_maker() as db:
        owner = await resolve_instance_job_organization(
            db,
            cluster_id=host,
            project=project,
        )
        if owner.resolution != "exact" or owner.organization_id is None:
            return False
        if Role.ADMIN in resolve_roles(actor):
            return True
        identity = actor.email or actor.user_id
        actor_org_ids = set(
            (
                await db.execute(
                    select(Organization.id)
                    .outerjoin(
                        OrganizationMember,
                        OrganizationMember.organization_id == Organization.id,
                    )
                    .where(
                        Organization.is_active == True,  # noqa: E712
                        or_(
                            OrganizationMember.user_email == identity,
                            Organization.created_by == identity,
                        ),
                    )
                )
            ).scalars()
        )
        return owner.organization_id in actor_org_ids


async def _authorize_operation(
    operation: Any, host: str, actor: AuthenticatedUser
) -> _OperationOwnership:
    ownership = _operation_ownership(operation)
    if ownership is None or not check_tenant_permission(
        host,
        ownership.project,
        ownership.instance_name or None,
        "view",
    ):
        raise _operation_not_found()
    try:
        permitted = await _authoritative_project_access(
            host=host,
            project=ownership.project,
            actor=actor,
        )
    except Exception as exc:  # noqa: BLE001 - ambiguity and DB failure both fail closed
        logger.error(
            "Could not resolve operation ownership for %s/%s: %s",
            host,
            ownership.project,
            type(exc).__name__,
        )
        raise _operation_not_found() from exc
    if not permitted:
        raise _operation_not_found()
    return ownership


def _operation_info(
    operation: Any,
    *,
    host: str,
    ownership: _OperationOwnership,
) -> "OperationInfo":
    return OperationInfo(
        id=str(_operation_value(operation, "id", "")),
        class_name=str(_operation_value(operation, "class", "") or ""),
        created_at=str(_operation_value(operation, "created_at", "") or ""),
        updated_at=str(_operation_value(operation, "updated_at", "") or ""),
        status=str(_operation_value(operation, "status", "") or ""),
        status_code=int(_operation_value(operation, "status_code", 0) or 0),
        description=sanitize_error(
            str(_operation_value(operation, "description", "") or ""), limit=500
        ),
        err=sanitize_error(str(_operation_value(operation, "err", "") or ""), limit=500),
        may_cancel=bool(_operation_value(operation, "may_cancel", False)),
        metadata=_sanitize_operation_metadata(_operation_value(operation, "metadata", {})),
        node_name=host,
        project=ownership.project,
        instance_name=ownership.instance_name,
    )


def _require_safe_cancellation(operation: Any) -> None:
    """Default-deny cancellation classes that are not ordinary project-owned tasks.

    Migration jobs will own cancellation through their durable job API. Until that ownership
    model exists here, this legacy endpoint only accepts a daemon task whose resource ownership
    was independently derived and whose daemon flag explicitly permits cancellation.
    """
    if str(_operation_value(operation, "class", "") or "") != "task":
        raise HTTPException(status_code=409, detail="This operation class cannot be cancelled")
    if not bool(_operation_value(operation, "may_cancel", False)):
        raise HTTPException(status_code=409, detail="This operation cannot be cancelled")


class OperationInfo(BaseModel):
    """Operation information."""
    id: str = Field(..., description="Operation ID")
    class_name: str = Field("", description="Operation class")
    created_at: str = Field("", description="Creation timestamp")
    updated_at: str = Field("", description="Last update timestamp")
    status: str = Field("", description="Operation status")
    status_code: int = Field(0, description="Status code")
    description: str = Field("", description="Operation description")
    err: str = Field("", description="Error message if any")
    may_cancel: bool = Field(False, description="Whether operation can be cancelled")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Operation metadata")
    node_name: str = Field("", description="Cluster where the operation is running")
    project: str = Field("default", description="Project for this operation")
    instance_name: str = Field("", description="Instance name if applicable")


class OperationListResponse(BaseModel):
    """Response for listing operations."""
    total: int = Field(..., description="Total number of operations")
    operations: List[OperationInfo] = Field(..., description="List of operations")
    nodes: List[str] = Field(..., description="Clusters included in this response")
    unreachable_nodes: List[str] = Field(
        default_factory=list,
        description=(
            "Clusters that could not be reached. Their entries are missing from this list, "
            "so an unreachable cluster is distinguishable from one that has none."
        ),
    )
    node_errors: Dict[str, str] = Field(
        default_factory=dict,
        description="Reason per cluster in unreachable_nodes. Empty for Tenant callers.",
    )


def _fetch_operations_for_node(lxd_service: LXDService, node_name: str) -> List[OperationInfo]:
    """Fetch operations for a single node - runs in thread pool."""
    operations = []
    try:
        client = lxd_service.connection_manager.get_client(node_name)
        if client is None:
            # important: unreachable is not empty. Without this the next line
            # raises AttributeError on None, which the generic handler below
            # turns into a partial result and the node is never named.
            raise LXDConnectionError(f"Could not connect to {node_name}")
        # Use recursion=1 to get full operation details
        response = client.api.operations.get(params={'recursion': '1'}, timeout=(5, 30))
        if response.status_code == 200:
            operations_data = response.json().get('metadata', {})
            # Operations are returned in 'running' and 'success' keys
            running_ops = operations_data.get('running', [])
            
            for op_data in running_ops:
                try:
                    ownership = _operation_ownership(op_data)
                    # Cluster-global, unknown and ambiguous resources are not project-owned and
                    # therefore have no safe row on this organization-scoped endpoint.
                    if ownership is None:
                        continue
                    operations.append(
                        _operation_info(op_data, host=node_name, ownership=ownership)
                    )
                except LXDConnectionError:
                    # a missing client IS unreachability: let it through
                    raise
                except Exception as e:
                    # important: not unreachability. See networks.py: an item
                    # vanishing mid-listing must not condemn the whole node.
                    logger.warning(f"Error parsing operation data on {node_name}: {e}")
        else:
            logger.warning(f"Got status {response.status_code} for operations on {node_name}")
    except LXDConnectionError:
        # a missing client IS unreachability: let it through
        raise
    except Exception as e:
        # important: not unreachability. See networks.py: an item
        # vanishing mid-listing must not condemn the whole node.
        logger.warning(f"Error listing operations on {node_name}: {e}", exc_info=True)
    return operations


@router.get(
    "",
    response_model=OperationListResponse,
    summary="List all operations",
    description="Get a list of all operations from connected clusters.",
)
async def list_operations(
    host: Optional[str] = Query(None, description="Filter by a specific cluster"),
    lxd_service: LXDService = Depends(get_lxd_service),
    actor: AuthenticatedUser = Depends(require_partner()),
):
    """List all operations."""
    try:
        if host:
            verify_cluster_access(host)
        nodes_to_query = [host] if host else lxd_service.connection_manager.get_node_names()
        
        # Fetch operations from all nodes in parallel
        loop = asyncio.get_event_loop()
        tasks = [
            loop.run_in_executor(_executor, _fetch_operations_for_node, lxd_service, node_name)
            for node_name in nodes_to_query
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Flatten results
        all_operations = []
        # important: two different things make a node absent, and both must be named.
        # A node can fail during the fan-out, or never be asked at all, because
        # connection_manager.get_node_names() returns only nodes with a live client while
        # the fleet is whatever Vault has registered. Reporting only the first kind left a
        # dead cluster silently missing rather than merely empty.
        unreachable_nodes: List[str] = (
            [] if host else sorted(set(lxd_service.get_node_names()) - set(nodes_to_query))
        )
        for node_name, result in zip(nodes_to_query, results):
            if isinstance(result, list):
                all_operations.extend(result)
            elif isinstance(result, Exception):
                logger.error(f"Error fetching operations: {result}")
                unreachable_nodes.append(node_name)

        authorized_operations: List[OperationInfo] = []
        for operation in all_operations:
            try:
                # Re-run ownership against the durable organization bindings. Project visibility
                # alone is insufficient for a shared cluster's ambiguous default project.
                permitted = check_tenant_permission(
                    operation.node_name,
                    operation.project,
                    operation.instance_name or None,
                    "view",
                ) and await _authoritative_project_access(
                    host=operation.node_name,
                    project=operation.project,
                    actor=actor,
                )
            except Exception:
                logger.error(
                    "Could not resolve listed operation ownership for %s/%s",
                    operation.node_name,
                    operation.project,
                )
                continue
            if permitted:
                authorized_operations.append(operation)
        all_operations = authorized_operations
        
        return OperationListResponse(
            total=len(all_operations),
            operations=all_operations,
            nodes=lxd_service.connection_manager.get_node_names(),
            unreachable_nodes=unreachable_nodes,
            node_errors=complete_reasons(unreachable_nodes, {}),
        )
    except HTTPException:
        # note: an authorization refusal is a 403, not a server fault. The generic handler
        # below would wrap it into a 500 whose body reads "403: Access denied ...", which
        # misleads clients and pages an on-call for a working permission check.
        raise
    except Exception as e:
        logger.error(f"Error listing operations: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{operation_id}",
    response_model=OperationInfo,
    summary="Get operation details",
    description="Get detailed information about a specific operation.",
)
async def get_operation(
    operation_id: str,
    host: str = Query(..., description="Target cluster"),
    lxd_service: LXDService = Depends(get_lxd_service),
    actor: AuthenticatedUser = Depends(require_partner()),
):
    """Get details for a specific operation."""
    verify_cluster_access(host)
    try:
        client = require_client(await client_for(lxd_service, host), host)
        operation = await _lookup_operation(client, operation_id, host)
    except HTTPException:
        # broad handler below converts it into a 500
        # important: let require_client's 503 through. Without this the
        # so an offline cluster does not read as a server fault.
        raise
    except Exception as exc:
        logger.info("Operation lookup on %s failed: %s", host, type(exc).__name__)
        raise _operation_not_found() from exc

    ownership = await _authorize_operation(operation, host, actor)
    return _operation_info(operation, host=host, ownership=ownership)


@router.delete(
    "/{operation_id}",
    response_model=dict,
    summary="Cancel operation",
    description="Cancel a running operation.",
)
async def cancel_operation(
    operation_id: str,
    host: str = Query(..., description="Target cluster"),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Cancel an operation."""
    verify_cluster_access(host)
    try:
        client = require_client(await client_for(lxd_service, host), host)
        operation = await _lookup_operation(client, operation_id, host)
    except HTTPException:
        raise
    except Exception as exc:
        logger.info("Operation lookup on %s failed: %s", host, type(exc).__name__)
        raise _operation_not_found() from exc

    ownership = await _authorize_operation(operation, host, _rbac)
    _require_safe_cancellation(operation)
    try:
        # The operation id can be absent from the journal in the small window after a daemon
        # accepted a migration submission and before the worker persisted its response. The
        # authoritative operation resources still identify the project/instance, whose durable
        # migration locks close that gap and also protect unknown submissions.
        await assert_project_not_migration_fenced(
            cluster_id=host,
            project=ownership.project,
        )
        if ownership.instance_name:
            await assert_instance_not_migration_fenced(
                cluster_id=host,
                project=ownership.project,
                instance_name=ownership.instance_name,
            )
        await assert_operation_not_migration_owned(
            cluster_id=host,
            operation_id=operation_id,
        )
    except MigrationFenceConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc

    try:
        # pylxd Operation objects don't have a cancel() method. Use the raw API only after the
        # independently fetched operation has proven one authorized project owner.
        await _operation_io(
            partial(
                client.api.operations[safe_segment(operation_id)].delete,
                timeout=_OPERATION_SOCKET_TIMEOUT,
            ),
            host=host,
            label=f"operations.cancel[{host}]",
            timeout=_OPERATION_CANCEL_TIMEOUT_SECONDS,
        )
        return {
            "success": True,
            "message": f"Operation {operation_id} cancelled successfully"
        }
    except HTTPException:
        # broad handler below converts it into a 500
        # important: let require_client's 503 through. Without this the
        # so an offline cluster does not read as a server fault.
        raise
    except BlockingPoolFull as exc:
        logger.warning("Operation cancellation capacity on %s is unavailable", host)
        raise HTTPException(
            status_code=503,
            detail={
                "code": "operation_cancel_temporarily_unavailable",
                "message": "Operation cancellation is temporarily unavailable; retry shortly.",
            },
        ) from exc
    except (BlockingTimeout, RequestsTimeout) as exc:
        # DELETE may have reached the daemon. Reporting an ordinary failure invites a blind
        # retry, while claiming success would be equally unsafe. The caller must refresh first.
        logger.warning("Operation cancellation outcome on %s is unknown", host)
        raise HTTPException(
            status_code=503,
            detail={
                "code": "operation_cancel_outcome_unknown",
                "message": (
                    "The cluster may have accepted the cancellation. Refresh the operation "
                    "before retrying."
                ),
            },
        ) from exc
    except Exception as exc:
        logger.error("Operation cancellation on %s failed: %s", host, type(exc).__name__)
        raise HTTPException(status_code=500, detail="Operation cancellation failed") from exc


@router.post(
    "/{operation_id}/wait",
    response_model=dict,
    summary="Wait for operation",
    description="Wait for an operation to complete.",
)
async def wait_operation(
    operation_id: str,
    host: str = Query(..., description="Target cluster"),
    timeout: Optional[int] = Query(30, ge=1, le=60, description="Timeout in seconds"),
    lxd_service: LXDService = Depends(get_lxd_service),
    actor: AuthenticatedUser = Depends(require_partner()),
):
    """Wait for an operation to complete."""
    verify_cluster_access(host)
    try:
        client = require_client(await client_for(lxd_service, host), host)
        operation = await _lookup_operation(client, operation_id, host)
    except HTTPException:
        raise
    except Exception as exc:
        logger.info("Operation lookup on %s failed: %s", host, type(exc).__name__)
        raise _operation_not_found() from exc

    await _authorize_operation(operation, host, actor)
    try:
        wait_seconds = int(timeout or 30)
        completed = await _wait_for_operation(
            client,
            operation_id,
            host,
            wait_seconds,
        )
        
        return {
            "success": True,
            "message": f"Operation {operation_id} completed",
            "status": _operation_value(
                completed,
                "status",
                _operation_value(operation, "status", ""),
            ),
            "status_code": _operation_value(
                completed,
                "status_code",
                _operation_value(operation, "status_code", 0),
            ),
        }
    except (BlockingTimeout, BlockingPoolFull, RequestsTimeout) as exc:
        logger.warning("Operation wait on %s timed out or was shed", host)
        raise HTTPException(
            status_code=503,
            detail="Operation wait is temporarily unavailable; refresh and retry.",
        ) from exc
    except Exception as exc:
        logger.error("Operation wait on %s failed: %s", host, type(exc).__name__)
        raise HTTPException(status_code=500, detail="Operation wait failed") from exc
