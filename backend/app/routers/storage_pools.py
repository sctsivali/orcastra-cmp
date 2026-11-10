"""
API Router for Storage Pool operations.
Provides endpoints for managing LXD storage pools.
"""

from fastapi import APIRouter, HTTPException, Depends, Query, Body
from typing import Optional, List, Dict
import logging
from datetime import datetime, timezone
import asyncio
from concurrent.futures import ThreadPoolExecutor

from app.services.lxd_service import (
    LXDConnectionError,
    LXDService,
    get_lxd_service,
)
from app.services.lxd import pool_resource_failures
from app.services.lxd.fanout import LATE_FRESH_SECONDS, NodeRead, collect, complete_reasons, gather_clusters, unattempted
from app.services.lxd.project_scope import ALL_PROJECTS_READ_TIMEOUT
from app.models.schemas import FanoutReachability
from app.core.dependencies import check_read_only_mode, require_tenant, require_partner, AuthenticatedUser
from app.core.org_scope import org_scope_dependency, verify_cluster_access, get_tenant_acl
from app.core.host_detail import pool_for_tier, pool_list_for_tier
from app.core.task_scope import caller_tier
from app.core.whole_cluster_access import verify_whole_cluster_access
from app.core.tenant_visibility import filter_visible_lxd_references, tenant_acl_active, tenant_acl_can_view_lxd_used_by
from app.core.cache import get_cache_manager, get_resource_list_generation
from pydantic import BaseModel, Field
from pylxd.exceptions import LXDAPIException
from app.services.activity_logger import get_activity_logger
from app.core.lxd_errors import lxd_http_error
from app.core.lxd_path import safe_segment
from app.services.lxd.utils import wait_for_lxd_operation, write_timeout
from app.services.lxd.cluster_topology import cluster_member_names, is_clustered
from app.core.database import get_db
from app.core import swr
from app.core.swr import Built
from app.services.shared_cluster_audit import serialize_cluster_configuration
from app.services.migration_fence import (
    MigrationFenceConflict,
    assert_cluster_not_migration_fenced,
)
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()


def _serialize_pool_resources(resources) -> dict:
    """Convert pylxd StorageResources to a plain dict.
    
    pylxd's StorageResources uses __slots__ and marshall() returns {},
    so FastAPI's jsonable_encoder produces {} instead of {space, inodes}.
    """
    return {
        "space": resources.space if isinstance(resources.space, dict) else {},
        "inodes": resources.inodes if isinstance(resources.inodes, dict) else {},
    }


# Storage pool keys LXD stores per member (lxd/db/cluster/storage_pools.go,
# NodeSpecificStorageConfig). On a cluster these go in the per-member define step; the daemon
# refuses them in the cluster-wide create and refuses every other key in the define.
MEMBER_SPECIFIC_POOL_KEYS = frozenset({
    "size",
    "source",
    "source.wipe",
    "volatile.initial_source",
    "zfs.pool_name",
    "lvm.thinpool_name",
    "lvm.vg_name",
})

# A body naming a known-down cluster is right today and may be wrong once the healer
# reconnects it, so it is remembered for one healer cadence rather than three minutes.
DEGRADED_CACHE_TTL = 60

router = APIRouter(
    prefix="/storage/pools",
    tags=["Storage Pools"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)

_executor = ThreadPoolExecutor(max_workers=5)
# Pool resource reads, one per cluster, on one module-level pool shared by every call.
_resources_executor = ThreadPoolExecutor(max_workers=10, thread_name_prefix="pool_resources")
# A pool write on LXD 6.9 is an operation; see NETWORK_WRITE_WAIT_SECONDS in networks.py.
POOL_WRITE_WAIT_SECONDS = 120


class StoragePoolInfo(BaseModel):
    """A storage pool on one cluster."""
    name: str = Field(..., description="Pool name")
    description: str = Field("", description="Pool description")
    driver: Optional[str] = Field(..., description="Storage driver (zfs, dir, btrfs, etc); null for a Tenant")
    config: Dict[str, str] = Field(default_factory=dict, description="Pool configuration; empty for a Tenant")
    used_by: List[str] = Field(default_factory=list, description="Resources using this pool")
    status: Optional[str] = Field("", description="Pool status; null for a Tenant")
    node: str = Field("", description="Cluster where the pool is defined")


def _pool_write_error(exc: Exception, pool_name: str) -> HTTPException:
    """The daemon's refusal as its real status. Anything else is a server fault."""
    if isinstance(exc, LXDAPIException):
        return lxd_http_error(exc, "Storage pool", pool_name)
    return HTTPException(status_code=500, detail=str(exc))


class StoragePoolCreateRequest(BaseModel):
    """Request body for creating a storage pool."""
    name: str = Field(..., description="Pool name")
    description: str = Field("", description="Pool description")
    driver: str = Field(..., description="Storage driver")
    config: Dict[str, str] = Field(default_factory=dict, description="Pool configuration")


class StoragePoolListResponse(FanoutReachability):
    """Response for listing storage pools."""
    total: int = Field(..., description="Total number of pools")
    pools: List[StoragePoolInfo] = Field(..., description="List of storage pools")
    nodes: List[str] = Field(..., description="Clusters included in this response")


def _fetch_pools_for_node(lxd_service: LXDService, node_name: str) -> NodeRead[StoragePoolInfo]:
    """Every storage pool on one cluster, in one request. Runs in the thread pool.

    A storage pool is a cluster-wide object, so one recursion=1 listing carries every pool
    with its driver, status and config (pylxd's manager lists at recursion 0 and syncs each
    pool afterwards). A failure of the listing itself propagates to the caller, which names the
    cluster rather than reporting it as having no pools.
    """
    client = lxd_service.get_client_with_reconnect(node_name)
    if client is None:
        # important: unreachable is not empty. Raise so the caller names the node; a bare
        # list would read as a node with no pools.
        raise LXDConnectionError(f"Could not connect to {node_name}")
    response = client.api.storage_pools.get(
        params={"recursion": 1}, timeout=ALL_PROJECTS_READ_TIMEOUT
    )
    read: NodeRead[StoragePoolInfo] = NodeRead()
    for row in response.json().get("metadata") or []:
        if not isinstance(row, dict):
            continue
        try:
            read.rows.append(StoragePoolInfo(
                name=row.get("name") or "",
                description=row.get("description") or "",
                driver=row.get("driver") or "",
                config=row.get("config") or {},
                used_by=row.get("used_by") or [],
                status=row.get("status") or "",
                node=node_name,
            ))
        except Exception as exc:  # noqa: BLE001 - one malformed row is named, not fatal
            read.lost(f"pool {row.get('name', '?')}: {type(exc).__name__}")
    return read


def _fetch_node_pool_resources(lxd_service: LXDService, tenant_acl, node_name: str):
    """Every visible pool's resources on one cluster. Runs in the thread pool.

    Returns None when the node could not be reached, so the caller can tell an outage apart
    from a node that has no pools.
    """
    result = {}
    try:
        client = lxd_service.get_client_with_reconnect(node_name)
        if client is None:
            logger.warning(f"Could not connect to {node_name}, skipping for resources")
            return None
        pools = client.storage_pools.all()
        for pool in pools:
            if not tenant_acl_can_view_lxd_used_by(tenant_acl, node_name, pool.used_by or []):
                continue
            try:
                resources = pool_resource_failures.read(node_name, pool.name, pool.resources.get)
                result[f"{node_name}-{pool.name}"] = _serialize_pool_resources(resources)
            except Exception as pool_err:
                logger.warning(f"Could not get resources for pool {pool.name} on {node_name}: {pool_err}")
    except Exception as node_err:
        logger.warning(f"Could not access node {node_name}: {node_err}")
    return result


async def _read_all_pool_resources(lxd_service: LXDService, tenant_acl) -> tuple[dict, int]:
    """Resources for every pool the caller may see, and how many clusters answered.

    Shared by /resources-all and the legacy -resources path. `tenant_acl` is read by the caller
    on the loop: contextvars do not cross into the threads.
    """
    nodes = lxd_service.connection_manager.get_node_names()
    if not nodes:
        # important: get_node_names() lists only nodes holding a live client, so an empty list
        # here means every cluster is down. It is not an answer and is never cached.
        return {"resources": {}}, 0
    loop = asyncio.get_event_loop()
    tasks = [
        loop.run_in_executor(_resources_executor, _fetch_node_pool_resources, lxd_service, tenant_acl, n)
        for n in nodes
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    all_resources = {}
    answered = 0
    for r in results:
        if isinstance(r, dict):
            answered += 1
            all_resources.update(r)
    return {"resources": all_resources}, answered


@router.get(
    "",
    response_model=StoragePoolListResponse,
    summary="List all storage pools",
    description="Get a list of all storage pools from connected clusters.",
)
async def list_storage_pools(
    host: Optional[str] = Query(None, description="Filter by a specific cluster"),
    lxd_service: LXDService = Depends(get_lxd_service),
    fresh: bool = False,
):
    """List all storage pools."""
    if host:
        verify_cluster_access(host)
    # 180 s soft TTL (60 s when a cluster is down), then served for SWR_MAX_STALE_SECONDS while
    # one background read replaces it.
    cache = get_cache_manager()

    async def build() -> Built:
        nodes_to_query = [host] if host else lxd_service.connection_manager.get_node_names()
        
        # CLUSTER_WALL_SECONDS per cluster (see fanout.gather_clusters)
        results = await gather_clusters(
            nodes_to_query,
            lambda node_name: _fetch_pools_for_node(lxd_service, node_name),
            executor=_executor,
            label="storage_pools.list",
            memo_key=f"storage_pools:host={host}",
            generation=lambda: get_resource_list_generation(cache, "storage_pools"),
        )
        
        # important: two things make a node absent, and both must be named. A node can fail
        # during the fan-out, or never be asked at all, because
        # connection_manager.get_node_names() returns only nodes with a live client while the
        # fleet is whatever Vault has registered. Naming only the first hides a dead cluster.
        seeded: List[str] = (
            [] if host else sorted(set(lxd_service.get_node_names()) - set(nodes_to_query))
        )
        unreachable_nodes: List[str] = list(seeded)
        fan = collect(
            nodes_to_query, results, noun="storage pools", reasons_visible=not tenant_acl_active()
        )
        unreachable_nodes.extend(fan.unreachable_nodes)
        all_pools = fan.rows
        # important: the seeding names two kinds of cluster, and only one of them may be
        # cached. A cluster this worker tried and found down is a correct answer, cached with a
        # shorter TTL so recovery shows within the healer's cadence. A cluster this worker has
        # never tried, registered seconds ago, must not be: the cache is shared by every worker,
        # and the one that did connect it would serve "unreachable" for the whole TTL. See
        # fanout.unattempted.
        cacheable = fan.cacheable_despite_outages and not unattempted(
            seeded, lxd_service.connection_manager
        )
        ttl = LATE_FRESH_SECONDS if fan.late_nodes else (DEGRADED_CACHE_TTL if unreachable_nodes else 180)

        # used_by is held to the caller's bound for every role; only a tenant loses the row
        # when nothing they may see uses it.
        for pool in all_pools:
            pool.used_by = filter_visible_lxd_references(pool.node, pool.used_by)

        if tenant_acl_active():
            all_pools = [pool for pool in all_pools if pool.used_by]
        
        response = StoragePoolListResponse(
            total=len(all_pools),
            pools=all_pools,
            nodes=lxd_service.connection_manager.get_node_names(),
            unreachable_nodes=unreachable_nodes,
            partial_nodes=fan.partial_nodes,
            late_nodes=fan.late_nodes,
            node_errors=complete_reasons(
                unreachable_nodes, fan.node_errors, partial=fan.partial_nodes, late=fan.late_nodes
            ),
            collected_at=datetime.now(timezone.utc),
        )

        # important: cache neither an outage nor a partial answer, or a cluster that failed its
        # listing would be cached as "no pools".
        if not cacheable:
            logger.warning("Not caching storage pools list: %s", fan.node_errors or "no node answered")
        return Built(value=response, body=response.model_dump(), fresh_for=ttl if cacheable else None)

    try:
        served = await swr.serve(
            cache=cache,
            base_key=f"storage_pools:list:e1:host={host}",
            fresh=fresh,
            build=build,
            generation=lambda: get_resource_list_generation(cache, "storage_pools"),
            label="storage pools list",
        )
        return pool_list_for_tier(served.as_model(StoragePoolListResponse), caller_tier())
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing storage pools: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/resources-all",
    summary="Get all storage pool resources",
    description="Get resources (space, inodes) for all storage pools across all clusters.",
)
async def get_all_storage_pool_resources(
    lxd_service: LXDService = Depends(get_lxd_service),
    fresh: bool = False,

    # important: a pool's whole size and usage count every party's volumes. Partner and above,
    # as for the monitoring dashboard's pools.
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Get resources for all storage pools on all nodes."""
    cache = get_cache_manager()

    async def build() -> Built:
        response, answered = await _read_all_pool_resources(lxd_service, get_tenant_acl())
        # important: cache only when a cluster answered. Answers are counted rather than
        # entries: a reachable fleet with no pools is empty, a fleet that is down is not.
        if not answered:
            logger.warning("Not caching storage pool resources: no node answered")
        return Built(value=response, body=response, fresh_for=60 if answered else None)

    try:
        served = await swr.serve(
            cache=cache,
            base_key="storage_pool_resources:all:e1",
            fresh=fresh,
            build=build,
            generation=lambda: get_resource_list_generation(cache, "storage_pool_resources"),
            label="storage pool resources",
        )
        return served.body
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting storage pool resources: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{pool_name}",
    response_model=StoragePoolInfo,
    summary="Get storage pool details",
    description="Get detailed information about a specific storage pool.",
)
async def get_storage_pool(
    pool_name: str,
    host: str = Query(..., description="Target cluster"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get details for a specific storage pool."""
    verify_cluster_access(host)
    try:
        client = lxd_service.get_client_with_reconnect(host)
        if client is None:
            raise HTTPException(status_code=503, detail=f"Could not connect to {host}")
        pool = client.storage_pools.get(safe_segment(pool_name))
        # important: this router is mounted with require_tenant(), and the list endpoint both
        # filters used_by and drops a pool with nothing visible left. The detail route does the
        # same, or a tenant could read another organization's instance and volume references and
        # reach a pool absent from its own list. As in the sibling detail handlers, the denial is
        # derived from the filtered references, so the gate cannot be kept while the filter goes.
        used_by = filter_visible_lxd_references(host, pool.used_by or [])
        if tenant_acl_active() and not used_by:
            raise HTTPException(status_code=403, detail="Access denied to this storage pool")

        return pool_for_tier(StoragePoolInfo(
            name=pool.name,
            description=getattr(pool, 'description', '') or "",
            driver=pool.driver,
            config=pool.config or {},
            used_by=used_by,
            status=getattr(pool, 'status', ''),
            node=host,
        ), caller_tier())
    except HTTPException:
        # important: let an HTTPException through unchanged. This try raises its own, and the
        # broad handler below would turn a 503 (unreachable cluster) or a 403 (tenant ACL
        # denial) into a 500 or 404.
        raise
    except Exception as e:
        logger.error(f"Error getting storage pool {pool_name}: {e}")
        raise HTTPException(status_code=404, detail=f"Storage pool {pool_name} not found")


@router.post(
    "",
    response_model=dict,
    summary="Create storage pool",
    description="Create a new storage pool.",
)
async def create_storage_pool(
    request: StoragePoolCreateRequest,
    host: str = Query(..., description="Target cluster"),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Create a new storage pool."""
    verify_cluster_access(host)
    # important: a storage pool belongs to no project and serves every project on the cluster,
    # so a caller held to a bound changes one only where no other party holds the cluster.
    await verify_whole_cluster_access(host, db, _rbac)
    try:
        async with serialize_cluster_configuration(db, host):
            try:
                await assert_cluster_not_migration_fenced(cluster_id=host, db=db)
            except MigrationFenceConflict as exc:
                raise HTTPException(status_code=409, detail=exc.detail) from exc
            client = lxd_service.get_client_with_reconnect(host)
            if client is None:
                raise HTTPException(status_code=503, detail=f"Could not connect to {host}")
            if is_clustered(client):
                # important: on a cluster a pool must be defined on every member first
                # (POST ?target=<member>, carrying only the member-specific keys) and then
                # created cluster-wide with the rest; a single cluster-wide POST answers
                # 'Pool not pending on any node (use --target <node> first)'. The console sends
                # one config, so every member is defined with the same source and size, which is
                # how MicroCloud lays out its local pool.
                member_config = {
                    k: v for k, v in (request.config or {}).items()
                    if k in MEMBER_SPECIFIC_POOL_KEYS
                }
                cluster_config = {
                    k: v for k, v in (request.config or {}).items()
                    if k not in MEMBER_SPECIFIC_POOL_KEYS
                }
                for member in cluster_member_names(client):
                    wait_for_lxd_operation(
                        client,
                        client.api.storage_pools.post(
                            json={
                                "name": request.name,
                                "driver": request.driver,
                                "config": member_config,
                            },
                            params={"target": member},
                            timeout=write_timeout(POOL_WRITE_WAIT_SECONDS),
                        ),
                        timeout=POOL_WRITE_WAIT_SECONDS,
                    )
                # LXD 6.9 answers with an operation (storage_and_network_operations); reading the
                # pool back before it settles is a 404 for a pool the daemon is still creating.
                wait_for_lxd_operation(
                    client,
                    client.api.storage_pools.post(
                        json={
                            "name": request.name,
                            "driver": request.driver,
                            "config": cluster_config,
                            "description": request.description,
                        },
                        timeout=write_timeout(POOL_WRITE_WAIT_SECONDS),
                    ),
                    timeout=POOL_WRITE_WAIT_SECONDS,
                )
                pool = client.storage_pools.get(safe_segment(request.name))
            else:
                pool = client.storage_pools.create({
                    "name": request.name,
                    "driver": request.driver,
                    "config": request.config,
                    "description": request.description
                })
        
        activity_logger.log_activity(
            action="storage.pool.create",
            target_type="storage_pool",
            target_id=request.name,
            target_host=host,
            result="success",
            details={"driver": request.driver, "description": request.description}
        )
        
        # Readers hold a cached list; without this the change is invisible until the TTL.
        get_cache_manager().invalidate_resource_cache("storage_pools", "storage_pool_resources")
        return {
            "success": True,
            "message": f"Storage pool {request.name} created successfully",
            "pool": StoragePoolInfo(
                name=pool.name,
                description=getattr(pool, 'description', '') or "",
                driver=pool.driver,
                config=pool.config or {},
                used_by=pool.used_by or [],
                status=getattr(pool, 'status', ''),
                node=host
            )
        }
    except HTTPException:
        # important: let an HTTPException through unchanged. This try raises its own, and the
        # broad handler below would turn a 503 (unreachable cluster) or a 403 (tenant ACL
        # denial) into a 500 or 404.
        raise
    except Exception as e:
        logger.error(f"Error creating storage pool: {e}")
        activity_logger.log_activity(
            action="storage.pool.create",
            target_type="storage_pool",
            target_id=request.name,
            target_host=host,
            result="failure",
            error_message=str(e)
        )
        raise _pool_write_error(e, request.name)


@router.put(
    "/{pool_name}",
    response_model=dict,
    summary="Update storage pool",
    description="Update an existing storage pool.",
)
async def update_storage_pool(
    pool_name: str,
    host: str = Query(..., description="Target cluster"),
    config: Optional[Dict[str, str]] = Body(None),
    description: Optional[str] = Body(None),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Update a storage pool."""
    verify_cluster_access(host)
    # important: a storage pool belongs to no project and serves every project on the cluster,
    # so a caller held to a bound changes one only where no other party holds the cluster.
    await verify_whole_cluster_access(host, db, _rbac)
    try:
        async with serialize_cluster_configuration(db, host):
            try:
                await assert_cluster_not_migration_fenced(cluster_id=host, db=db)
            except MigrationFenceConflict as exc:
                raise HTTPException(status_code=409, detail=exc.detail) from exc
            client = lxd_service.get_client_with_reconnect(host)
            if client is None:
                raise HTTPException(status_code=503, detail=f"Could not connect to {host}")
            pool = client.storage_pools.get(safe_segment(pool_name))

            if config is not None:
                pool.config = config
            if description is not None and hasattr(pool, 'description'):
                pool.description = description

            pool.save(wait=True)
        
        # Readers hold a cached list; without this the change is invisible until the TTL.
        get_cache_manager().invalidate_resource_cache("storage_pools", "storage_pool_resources")
        return {
            "success": True,
            "message": f"Storage pool {pool_name} updated successfully"
        }
    except HTTPException:
        # important: let an HTTPException through unchanged. This try raises its own, and the
        # broad handler below would turn a 503 (unreachable cluster) or a 403 (tenant ACL
        # denial) into a 500 or 404.
        raise
    except Exception as e:
        logger.error(f"Error updating storage pool {pool_name}: {e}")
        raise _pool_write_error(e, pool_name)


@router.delete(
    "/{pool_name}",
    response_model=dict,
    summary="Delete storage pool",
    description="Delete a storage pool.",
)
async def delete_storage_pool(
    pool_name: str,
    host: str = Query(..., description="Target cluster"),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Delete a storage pool."""
    verify_cluster_access(host)
    # important: a storage pool belongs to no project and serves every project on the cluster,
    # so a caller held to a bound changes one only where no other party holds the cluster.
    await verify_whole_cluster_access(host, db, _rbac)
    try:
        async with serialize_cluster_configuration(db, host):
            try:
                await assert_cluster_not_migration_fenced(cluster_id=host, db=db)
            except MigrationFenceConflict as exc:
                raise HTTPException(status_code=409, detail=exc.detail) from exc
            client = lxd_service.get_client_with_reconnect(host)
            if client is None:
                raise HTTPException(status_code=503, detail=f"Could not connect to {host}")
            pool = client.storage_pools.get(safe_segment(pool_name))
            pool.delete(wait=True)
        
        activity_logger.log_activity(
            action="storage.pool.delete",
            target_type="storage_pool",
            target_id=pool_name,
            target_host=host,
            result="success"
        )
        
        # Readers hold a cached list; without this the change is invisible until the TTL.
        get_cache_manager().invalidate_resource_cache("storage_pools", "storage_pool_resources")
        return {
            "success": True,
            "message": f"Storage pool {pool_name} deleted successfully"
        }
    except HTTPException:
        # important: let an HTTPException through unchanged. This try raises its own, and the
        # broad handler below would turn a 503 (unreachable cluster) or a 403 (tenant ACL
        # denial) into a 500 or 404.
        raise
    except Exception as e:
        logger.error(f"Error deleting storage pool {pool_name}: {e}")
        activity_logger.log_activity(
            action="storage.pool.delete",
            target_type="storage_pool",
            target_id=pool_name,
            target_host=host,
            result="failure",
            error_message=str(e)
        )
        raise _pool_write_error(e, pool_name)


# Its own function name: sharing get_all_storage_pool_resources with the /resources-all handler
# would shadow that function for anything importing it by name.
@router.get(
    "-resources",
    response_model=dict,
    summary="Get all storage pool resources",
    description="Get resource usage information for all storage pools across all clusters.",
)
async def get_all_storage_pool_resources_legacy_path(
    lxd_service: LXDService = Depends(get_lxd_service),
    fresh: bool = False,

    # important: a pool's whole size and usage count every party's volumes. Partner and above,
    # as for the monitoring dashboard's pools.
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Get storage pool resources for all pools."""
    cache = get_cache_manager()

    async def build() -> Built:
        response, answered = await _read_all_pool_resources(lxd_service, get_tenant_acl())
        # important: cache only when a cluster answered. Answers are counted rather than
        # entries: a reachable fleet with no pools is empty, a fleet that is down is not.
        if not answered:
            logger.warning("Not caching storage pool resources: no node answered")
        return Built(value=response, body=response, fresh_for=60 if answered else None)

    try:
        served = await swr.serve(
            cache=cache,
            base_key="storage_pool_resources:all_v2:e1",
            fresh=fresh,
            build=build,
            generation=lambda: get_resource_list_generation(cache, "storage_pool_resources"),
            label="storage pool resources (legacy path)",
        )
        return served.body
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting storage pool resources: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{pool_name}/resources",
    response_model=dict,
    summary="Get storage pool resources",
    description="Get resource usage information for a specific storage pool.",
)
async def get_storage_pool_resources(
    pool_name: str,
    host: str = Query(..., description="Target cluster"),
    lxd_service: LXDService = Depends(get_lxd_service),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Get storage pool resources."""
    verify_cluster_access(host)
    try:
        client = lxd_service.get_client_with_reconnect(host)
        if client is None:
            raise HTTPException(status_code=503, detail=f"Could not connect to {host}")
        pool = client.storage_pools.get(safe_segment(pool_name))
        if not tenant_acl_can_view_lxd_used_by(get_tenant_acl(), host, pool.used_by or []):
            raise HTTPException(status_code=403, detail="Access denied to this storage pool")
        resources = pool.resources.get()
        
        return {
            "name": pool_name,
            "resources": _serialize_pool_resources(resources)
        }
    except HTTPException:
        # important: let an HTTPException through unchanged. This try raises its own, and the
        # broad handler below would turn a 503 (unreachable cluster) or a 403 (tenant ACL
        # denial) into a 500 or 404.
        raise
    except Exception as e:
        logger.error(f"Error getting storage pool resources {pool_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
