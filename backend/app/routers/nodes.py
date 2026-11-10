"""
API Router for LXD node status and health checks.
Provides endpoints for monitoring node connectivity.
"""

from fastapi import APIRouter, Depends, HTTPException, Response
import logging
import asyncio
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

from app.models.schemas import (
    ClusterAvailabilityResponse,
    NodesStatusResponse,
    NodeResources,
    NodeUpdateRequest,
    NodeUpdateResponse,
    GPUCardInfo,
    GpuCardListResponse,
)
from app.services.lxd import status as lxd_status
from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking
from app.services.lxd_service import (
    LXDService,
    get_lxd_service,
    LXDOperationError,
)
from app.core import swr
from app.services.hypervisor_releases import service as hypervisor_releases
from app.core.swr import Built
from app.core.config import get_settings
from app.core.dependencies import check_read_only_mode, require_tenant, require_partner, require_admin, AuthenticatedUser
from app.core.org_scope import (
    get_scope_cache_key,
    org_scope_dependency,
    verify_cluster_access,
    visible_projects_for,
)
from app.core.rbac import Role, get_highest_role, resolve_roles
from app.core.database import get_db
from app.core.whole_cluster_access import verify_whole_cluster_access
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.vault_client import get_vault_client
from app.core import vault_status
from app.core.registry_guard import require_known_registry, vault_unavailable_error
from app.core.org_scope import apply_cluster_filter
from app.services.lxd_certs.storage import RegistryRead, stored_location_fields
from app.core.cache import (
    CAPACITY_GENERATION_KIND,
    get_cache_manager,
    get_resource_list_generation,
    NODES_AVAILABILITY_CACHE_KEY,
    NODES_STATUS_CACHE_KEY,
    REGIONS_OVERVIEW_SWR_KEY,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/nodes",
    tags=["Clusters"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)

# Thread pool for parallel node status fetching
_node_executor = ThreadPoolExecutor(max_workers=10)

# Availability probes get their own pool so a slow reachability sweep cannot starve the
# node-status fan-out, and vice versa.
_availability_executor = ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="nodes_availability"
)
# One connect budget (3s) plus one read budget (8s) plus margin. The probes run in parallel,
# so this bounds the whole sweep.
NODES_AVAILABILITY_PROBE_TIMEOUT = 20.0
# The Vault LIST budget (3s connect, 10s read, see core/vault_client.VAULT_LIST_TIMEOUT) plus
# margin. Past it the request is answered from the last known registry, never left waiting.
REGISTRY_READ_TIMEOUT = 12.0


async def _read_registry(lxd_service: LXDService) -> RegistryRead:
    """The registered cluster ids for this request, read off the event loop.

    The Vault LIST runs on the availability pool with its own budget, so a slow Vault cannot
    hold the worker; on expiry or a full pool the last known registry answers. The org-scope
    filter runs after either path, so the last known registry is scoped too.
    """
    try:
        read = await run_blocking(
            lxd_service.cert_manager.read_registry,
            timeout=REGISTRY_READ_TIMEOUT,
            label="nodes.registry",
            executor=_availability_executor,
            max_in_flight=4,
        )
    except (BlockingTimeout, BlockingPoolFull) as exc:
        logger.warning("Cluster registry read unavailable: %s", exc)
        known = vault_status.last_known_registry()
        read = RegistryRead(
            cluster_ids=known or [], source="last_known" if known is not None else None, readable=False
        )
    return RegistryRead(
        cluster_ids=apply_cluster_filter(read.cluster_ids), source=read.source, readable=read.readable
    )

def _availability_for_user(
    response: ClusterAvailabilityResponse,
    user: AuthenticatedUser,
) -> ClusterAvailabilityResponse:
    """Remove control-plane metadata from the Tenant availability projection.

    Build new models rather than mutating rows returned from Redis. The same cached body may be
    served to a Partner later in the process and must retain its endpoint, hypervisor type, and
    curated operator-facing reason.
    """
    if get_highest_role(resolve_roles(user)) in (Role.ADMIN, Role.PARTNER):
        return response
    return response.model_copy(
        update={
            "clusters": [
                row.model_copy(
                    update={"endpoint": None, "node_type": None, "reason": None}
                )
                for row in response.clusters
            ]
        }
    )


def _resources_for_project_scope(
    node_name: str,
    resources: NodeResources | None,
) -> NodeResources | None:
    """Redact GPU owners when a Partner is project-bound on a shared cluster.

    GPU discovery records only an instance name. Without its project, the server cannot prove
    that name belongs to a project this Partner may inspect. Return fresh models so neither a
    cached node response nor the shared LXD inventory is mutated for the next request.
    """
    if resources is None or visible_projects_for(node_name) is None:
        return resources
    return resources.model_copy(
        update={
            "gpu_cards": _gpu_cards_for_project_scope(
                node_name,
                resources.gpu_cards,
            )
        }
    )


def _gpu_cards_for_project_scope(
    node_name: str,
    cards: list[GPUCardInfo],
) -> list[GPUCardInfo]:
    """Return project-safe GPU card copies without rebuilding NodeResources."""
    if visible_projects_for(node_name) is None:
        return cards
    return [card.model_copy(update={"used_by": None}) for card in cards]


def _nodes_for_project_scope(response: NodesStatusResponse) -> NodesStatusResponse:
    """Apply project-safe resource projection to every node, including cached bodies."""
    return response.model_copy(
        update={
            "nodes": [
                node.model_copy(
                    update={
                        "resources": _resources_for_project_scope(
                            node.name,
                            node.resources,
                        )
                    }
                )
                for node in response.nodes
            ]
        }
    )



@router.get(
    "",
    response_model=NodesStatusResponse,
    summary="Get Cluster status",
    description="Check connectivity and status for every registered Cluster.",
    responses={
        200: {"description": "Cluster status retrieved successfully"},
    }
)
async def get_nodes_status(
    lxd_service: LXDService = Depends(get_lxd_service),
    fresh: bool = False,
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Get the status of every registered Cluster."""
    # The 180 s soft TTL is the client's poll interval too; keep the two in step. Past it the
    # body is still served for SWR_MAX_STALE_SECONDS while one background read replaces it.
    cache = get_cache_manager()

    async def build() -> Built:
        statuses = await lxd_service.check_nodes_status_async()
        # important: check before anything is cached, or an unreadable registry with nothing
        # known is cached for 180 s as an empty fleet with all_connected=True.
        require_known_registry([status.name for status in statuses])

        # No second Vault sweep is needed: check_nodes_status_async already iterates the same
        # apply_cluster_filter(cert_manager.list_clusters()) set.

        # important: stamped here, where the fan-out finished, and carried through the cache
        # verbatim. Restamping on a cached read would report a three-minute-old body as current.
        response = NodesStatusResponse(
            nodes=statuses,
            all_connected=all(node.connected for node in statuses),
            collected_at=datetime.now(timezone.utc),
        )
        return Built(value=response, body=response.model_dump(), fresh_for=180)

    served = await swr.serve(
        cache=cache,
        base_key=NODES_STATUS_CACHE_KEY,
        fresh=fresh,
        build=build,
        generation=lambda: get_resource_list_generation(cache, CAPACITY_GENERATION_KIND),
        label="cluster status",
    )
    response = _nodes_for_project_scope(served.as_model(NodesStatusResponse))
    # Attached after the cache rather than inside build(): the verdict follows the release feed,
    # which changes on its own clock, so a cached body still gets the current answer.
    await hypervisor_releases.attach(response.nodes)
    return response



@router.get(
    "/names",
    response_model=list[str],
    summary="Get Cluster identifiers",
    description=(
        "Get configured Cluster identifiers. Carries no reachability: use "
        "/nodes/availability when the caller has to know which clusters will answer."
    ),
    deprecated=True,
)
async def get_node_names(
    response: Response,
    lxd_service: LXDService = Depends(get_lxd_service),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Get configured Cluster identifiers.

    The body is a bare list for the API-key clients of this deprecated route. When the registry
    cannot be read, the last known fleet is returned with `X-Orcastra-Registry-Readable: false`,
    or 503 when nothing is known.
    """
    read = await _read_registry(lxd_service)
    if read.readable:
        return read.cluster_ids
    if read.source is None:
        raise vault_unavailable_error()
    response.headers["X-Orcastra-Registry-Readable"] = "false"
    response.headers["Cache-Control"] = "no-store"
    return read.cluster_ids


# important: declared before any /{node_name}/... route. FastAPI matches in declaration order,
# so moving this below /{node_name}/resources makes "availability" a node name and this
# endpoint unreachable.
@router.get(
    "/availability",
    response_model=ClusterAvailabilityResponse,
    response_model_exclude_none=True,
    summary="Get cluster availability",
    description=(
        "Per-cluster reachability for node pickers. Cheap by construction: it rides the cached "
        "/nodes fan-out when one is warm and falls back to a reachability-only probe, never to "
        "the resource fan-out."
    ),
)
async def get_nodes_availability(
    lxd_service: LXDService = Depends(get_lxd_service),
    fresh: bool = False,
    _rbac: AuthenticatedUser = Depends(require_tenant()),
):
    """Availability of every cluster in the caller's org scope.

    A cluster that is down is reported in the payload, never as an error. The endpoint answers
    503 only when the registry cannot be read and nothing is known, or when the probe itself is
    unavailable.
    """
    try:
        response = await lxd_status.resolve_availability(lxd_service, fresh=fresh)
        return _availability_for_user(response, _rbac)
    except lxd_status.RegistryUnavailable:
        # A lost registry with nothing known: the probe would read an empty fleet and report
        # it as "no clusters". While the last known registry is served the probe runs over it.
        raise vault_unavailable_error()
    except (BlockingTimeout, BlockingPoolFull) as exc:
        # Answer 503 rather than marking every cluster offline: a slow probe must not paint a
        # healthy fleet as dead.
        logger.warning("Availability probe unavailable: %s", exc)
        raise HTTPException(
            status_code=503, detail="Cluster reachability is temporarily unavailable"
        )


@router.get(
    "/{node_name}/resources",
    response_model=NodeResources,
    summary="Get Cluster resources",
    description="Get CPU, Memory, and Storage capacity for a specific Cluster.",
)
async def get_node_resources(
    node_name: str,
    lxd_service: LXDService = Depends(get_lxd_service),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Get available resources for a specific registered Cluster."""
    verify_cluster_access(node_name)
    try:
        resources = await asyncio.to_thread(lxd_service.get_node_resources_detail, node_name)
        return _resources_for_project_scope(node_name, resources)
    except HTTPException:
        raise
    except LXDOperationError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get(
    "/{node_name}/gpus",
    response_model=GpuCardListResponse,
    summary="Get Cluster GPUs",
    description="List the physical GPUs available on a specific Cluster for GPU passthrough.",
)
async def get_node_gpus(
    node_name: str,
    lxd_service: LXDService = Depends(get_lxd_service),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """List the GPUs on a specific registered Cluster for the passthrough picker."""
    verify_cluster_access(node_name)
    try:
        # get_node_gpus makes blocking LXD calls; keep them off the event loop.
        inventory = await asyncio.to_thread(lxd_service.get_node_gpus, node_name)
        # Every detected card is returned, display controllers included. Each carries its class
        # and an `attachable` flag so the picker can rank and mark them instead of hiding
        # hardware the operator can see in LXD.
        cards = _gpu_cards_for_project_scope(node_name, inventory.cards)
        return GpuCardListResponse(
            host=node_name,
            gpus=cards,
            total=inventory.gpu_cards_total,
            compute_total=inventory.gpu_total,
        )
    except HTTPException:
        raise
    except LXDOperationError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch(
    "/{node_name}",
    response_model=NodeUpdateResponse,
    summary="Update Cluster metadata",
    description="Update a Cluster display name and other metadata.",
    responses={
        200: {"description": "Cluster metadata updated successfully"},
        404: {"description": "Cluster not found"},
        400: {"description": "Invalid request"},
    }
)
async def update_node(
    node_name: str,
    request: NodeUpdateRequest,
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Update Cluster metadata such as its display name."""
    verify_cluster_access(node_name)
    # important: the display name belongs to the whole cluster and is shown to every holder and
    # to admins, so renaming it needs whole-cluster access.
    await verify_whole_cluster_access(node_name, db, _rbac)
    from fastapi import HTTPException
    from app.core.node_metadata import get_node_metadata_store

    node_exists = False
    in_vault = False

    if node_name in lxd_service.get_node_names():
        node_exists = True
        in_vault = True  # get_node_names() sources from Vault via cert_manager.list_clusters()
    
    # Fallback: check Vault directly if not found
    if not node_exists:
        settings = get_settings()
        if settings.vault_enabled and settings.vault_token:
            try:
                from app.services.lxd_cert_manager import LXDCertificateManager
                vault_client = get_vault_client(settings.vault_addr, settings.vault_token)
                cert_manager = LXDCertificateManager(
                    vault_client=vault_client,
                    clusters_path=settings.vault_clusters_path
                )
                cluster_ids = cert_manager.list_clusters()
                if node_name in cluster_ids:
                    node_exists = True
                    in_vault = True
            except Exception as e:
                logger.warning(f"Failed to check Vault clusters: {e}")
    
    if not node_exists:
        raise HTTPException(
            status_code=404,
            detail=f"Cluster '{node_name}' not found"
        )
    
    try:
        metadata_store = get_node_metadata_store()
        metadata_store.set_node_display_name(node_name, request.display_name)

        if in_vault:
            settings = get_settings()
            if settings.vault_enabled and settings.vault_token:
                try:
                    from app.services.lxd_cert_manager import LXDCertificateManager
                    vault_client = get_vault_client(settings.vault_addr, settings.vault_token)
                    cert_manager = LXDCertificateManager(
                        vault_client=vault_client,
                        clusters_path=settings.vault_clusters_path
                    )
                    cluster_info = cert_manager.get_cluster_info(node_name)
                    metadata = cluster_info.get("metadata", {})
                    # Write back only over a complete record, keeping every stored field.
                    if metadata.get("endpoint"):
                        cert_manager.save_cluster_metadata(
                            cluster_id=node_name,
                            display_name=request.display_name,
                            endpoint=metadata.get("endpoint", ""),
                            zone=metadata.get("zone", ""),
                            # Preserve, never invent: an absent type means not determined yet.
                            cluster_type=metadata.get("type"),
                            merchant_id=metadata.get("merchant_id", ""),
                            # A location no reader can use is not written back; a
                            # complete one is re-saved with the keys it had.
                            location=stored_location_fields(node_name, metadata),
                        )
                        logger.info(f"Updated display name for Vault cluster {node_name}")
                    else:
                        logger.warning(f"Skipped Vault metadata update for {node_name}: existing metadata is empty/incomplete")
                except Exception as e:
                    logger.warning(f"Failed to update Vault metadata: {e}")
        
        # Invalidate caches so updated name appears immediately
        cache = get_cache_manager()
        if cache:
            cache.invalidate_resource_cache("clusters", "regions")
            cache.invalidate_capacity_cache()
            cache.invalidate_availability_cache()
            cache.invalidate_cluster_cache(node_name)
            logger.info(f"Invalidated caches after updating Cluster {node_name}")
        
        return NodeUpdateResponse(
            success=True,
            message=f"Cluster '{node_name}' updated successfully",
            node_name=node_name,
            display_name=request.display_name,
        )
    except Exception as e:
        logger.error(f"Error updating Cluster {node_name}: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.post(
    "/{node_name}/cache/invalidate",
    summary="Invalidate Cluster cache",
    description="Force a Cluster data refresh by invalidating cache entries after configuration changes.",
    responses={
        200: {"description": "Cache invalidated successfully"},
    }
)
async def invalidate_node_cache(
    node_name: str,
    lxd_service: LXDService = Depends(get_lxd_service),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Invalidate all cache entries for a specific registered Cluster."""
    verify_cluster_access(node_name)
    from fastapi import HTTPException
    
    try:
        cache = get_cache_manager()
        
        # These projections share generation tokens. Clear them through the helpers so an
        # in-flight read cannot write its pre-invalidation result back afterwards.
        scope_keys = tuple(get_scope_cache_key(key) for key in (
            NODES_STATUS_CACHE_KEY, NODES_AVAILABILITY_CACHE_KEY, REGIONS_OVERVIEW_SWR_KEY,
        ))
        await run_blocking(
            lambda: cache.invalidate_node_cache(node_name, scope_keys),
            timeout=20.0, label="nodes.cache.invalidate", executor=_availability_executor,
            max_in_flight=4,
        )
        
        logger.info(f"Invalidated all cache entries for Cluster: {node_name}")
        
        return {
            "success": True,
            "message": f"Cache invalidated for Cluster '{node_name}'",
            "node_name": node_name
        }
    except Exception as e:
        logger.error(f"Error invalidating cache for {node_name}: {e}")
        raise HTTPException(status_code=503, detail="Cluster cache refresh was incomplete. Retry the refresh.") from e


@router.post(
    "/cache/invalidate-all",
    summary="Invalidate all Cluster caches",
    description="Force a refresh by clearing every registered Cluster cache entry.",
    responses={
        200: {"description": "All caches invalidated successfully"},
    }
)
async def invalidate_all_nodes_cache(
    lxd_service: LXDService = Depends(get_lxd_service),
    _rbac: AuthenticatedUser = Depends(require_admin()),
):
    """Invalidate every registered Cluster cache entry."""
    from fastapi import HTTPException
    
    try:
        cache = get_cache_manager()
        
        await run_blocking(
            lambda: cache.invalidate_all_node_caches(),
            timeout=20.0, label="nodes.cache.invalidate_all", executor=_availability_executor,
            max_in_flight=4,
        )
        
        logger.info("Invalidated all Cluster cache entries")
        
        return {
            "success": True,
            "message": "All Cluster caches invalidated successfully"
        }
    except Exception as e:
        logger.error(f"Error invalidating all caches: {e}")
        raise HTTPException(
            status_code=503, detail="Fleet cache refresh was incomplete. Retry the refresh.",
        ) from e
