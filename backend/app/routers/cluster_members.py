"""
API Router for MicroCloud cluster members.
Provides endpoints for listing cluster members in a MicroCloud setup.
"""

from fastapi import APIRouter, HTTPException, Depends, Query
from typing import Any, Dict, List, Optional, Tuple
import asyncio
import logging
from datetime import datetime, timezone

from app.services.lxd_service import (
    LXDService,
    get_lxd_service,
    require_client,
)
from pydantic import BaseModel, Field
from pylxd.exceptions import LXDAPIException
from app.core.single_flight import single_flight
from app.core.cache import get_cache_manager, MEMBER_RESOURCES_CACHE_PREFIX
from app.core.config import get_settings
from app.models.schemas import (
    MemberResources, 
)

from app.services.lxd.gpu import build_gpu_inventory
from app.services.lxd.project_scope import list_instance_records
from app.services.lxd.node_resources import PoolReading, _read_pool_space
from app.services.lxd.storage_model import summarise_cluster_storage
from app.core.dependencies import require_tenant
from app.core.org_scope import (
    org_scope_dependency,
    verify_cluster_access,
    get_scope_cache_key,
    get_tenant_acl,
    project_is_visible,
)
from app.core.tenant_visibility import tenant_acl_has_permission
from app.core.lxd_names import safe_segment

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/cluster-members",
    tags=["Cluster Members"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)


class ClusterMember(BaseModel):
    """Information about a cluster member."""
    server_name: str = Field(..., description="Server name in the cluster")
    url: str = Field(..., description="Server URL/address")
    database: bool = Field(False, description="Whether this member has database role")
    status: str = Field("Online", description="Member status")
    message: str = Field("Fully operational", description="Status message")
    roles: List[str] = Field(default_factory=list, description="Roles assigned to this member")
    architecture: Optional[str] = Field(None, description="System architecture")
    failure_domain: Optional[str] = Field(None, description="Failure domain")
    description: Optional[str] = Field(None, description="Member description")
    groups: List[str] = Field(default_factory=list, description="Member groups")


class ClusterMembersResponse(BaseModel):
    """Response for listing cluster members."""
    node: str = Field(..., description="Node name being queried")
    is_clustered: bool = Field(..., description="Whether the node is part of a cluster")
    cluster_enabled: bool = Field(..., description="Whether clustering is enabled")
    members: List[ClusterMember] = Field(default_factory=list, description="List of cluster members")
    total: int = Field(0, description="Total number of members")


@router.get(
    "",
    response_model=ClusterMembersResponse,
    summary="List cluster members",
    description="Get list of all members in a hypervisor cluster. Only works for clustered nodes.",
)
async def list_cluster_members(
    node: str = Query(..., description="Node name to query for cluster members"),
    lxd_service: LXDService = Depends(get_lxd_service),
    fresh: bool = False,
):
    """List all cluster members for a given node."""
    verify_cluster_access(node)
    # Try cache first (30 second cache)
    cache = get_cache_manager()
    cache_key = get_scope_cache_key(f"cluster_members:list:host={node}")
    cached_data = None if fresh else cache.get("api_responses", cache_key)
    if cached_data is not None:
        logger.debug(f"Returning cached cluster members for host={node}")
        # Reconstruct Pydantic model from cached dict
        return ClusterMembersResponse(**cached_data)
    
    try:
        # Try to get client from connection manager first
        client = lxd_service.connection_manager.get_client(node)
        
        # If not found, try to connect using Vault certificate
        if client is None:
            logger.info(f"Node {node} not in connection manager, trying Vault...")
            from app.core.config import get_settings
            from app.core.vault_client import get_vault_client
            from app.services.lxd_cert_manager import LXDCertificateManager
            
            settings = get_settings()
            if settings.vault_enabled and settings.vault_token:
                try:
                    vault_client = get_vault_client(settings.vault_addr, settings.vault_token)
                    cert_manager = LXDCertificateManager(
                        vault_client=vault_client,
                        clusters_path=settings.vault_clusters_path
                    )
                    
                    # Get cluster info and certificate
                    cluster_info = cert_manager.get_cluster_info(node)
                    cert_data = cert_manager.get_cluster_certificate(node)
                    
                    # Connect with certificate
                    client = lxd_service.connection_manager.connect_with_vault_cert(
                        cluster_id=node,
                        endpoint=cluster_info["metadata"]["endpoint"],
                        cert_content=cert_data["client.crt"],
                        key_content=cert_data["client.key"],
                        verify=False
                    )
                    logger.info(f"Successfully connected to {node} via Vault certificate")
                except Exception as e:
                    logger.error(f"Failed to connect to {node} via Vault: {e}")
        
        # important: an unreachable cluster answers 503 here like everywhere else. This
        # used to return is_clustered=False, and the members page renders that as
        # "Not a Clustered Node", so an outage read as a configuration fact. The sibling
        # get_member_resources has always used require_client.
        client = require_client(client, node)

        # Check if clustering is enabled
        host_info = client.host_info
        environment = host_info.get("environment", {})
        is_clustered = environment.get("server_clustered", False)
        
        if not is_clustered:
            return ClusterMembersResponse(
                node=node,
                is_clustered=False,
                cluster_enabled=False,
                members=[],
                total=0
            )
        
        # Get cluster members
        try:
            # Use the cluster API endpoint with recursion to get full member data
            # Without recursion=1, LXD returns just member URLs instead of full objects
            response = client.api.cluster.members.get(params={"recursion": 1})
            response_data = response.json()
            
            # Handle different response formats
            if isinstance(response_data, dict):
                members_data = response_data.get("metadata", [])
            elif isinstance(response_data, list):
                members_data = response_data
            else:
                logger.warning(f"Unexpected response format: {type(response_data)}")
                members_data = []
            
            logger.info(f"Got {len(members_data)} cluster members from {node}")
            
            members = []
            for member_data in members_data:
                # Skip if member_data is not a dict
                if not isinstance(member_data, dict):
                    logger.warning(f"Skipping non-dict member data: {member_data}")
                    continue
                
                # Get roles directly from LXD API response
                # LXD returns roles as a list like ["database-leader", "database"] or ["database"]
                roles = member_data.get("roles", [])
                
                member = ClusterMember(
                    server_name=member_data.get("server_name", "unknown"),
                    url=member_data.get("url", ""),
                    database=member_data.get("database", False),
                    status=member_data.get("status", "Online"),
                    message=member_data.get("message", "Fully operational"),
                    roles=roles,
                    architecture=member_data.get("architecture"),
                    failure_domain=member_data.get("failure_domain", "default"),
                    description=member_data.get("description"),
                    groups=member_data.get("groups", [])
                )
                members.append(member)
                logger.debug(f"Added member: {member.server_name} with roles: {roles}")
            
            # Create response
            response = ClusterMembersResponse(
                node=node,
                is_clustered=True,
                cluster_enabled=True,
                members=members,
                total=len(members)
            )
            
            # Cache as dict for 120 seconds
            cache.set("api_responses", cache_key, response.model_dump(), ttl=180)
            logger.debug("Cached cluster members for 120 seconds")
            
            return response
            
        except Exception as e:
            logger.error(f"Error fetching cluster members from {node}: {e}")
            # If we can't get members but it's clustered, return empty list
            return ClusterMembersResponse(
                node=node,
                is_clustered=True,
                cluster_enabled=True,
                members=[],
                total=0
            )
        
    except HTTPException:
        # important: let require_client's 503 through. Without this it is caught two
        # lines later and re-raised as a 500.
        raise
    except Exception as e:
        logger.error(f"Error getting cluster members for {node}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{member_name}",
    response_model=ClusterMember,
    summary="Get cluster member details",
    description="Get detailed information about a specific cluster member.",
)
async def get_cluster_member(
    member_name: str,
    node: str = Query(..., description="Node name to query"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get details for a specific cluster member."""
    verify_cluster_access(node)
    try:
        client = require_client(lxd_service.connection_manager.get_client(node), node)
        
        # Get specific member
        response = client.api.cluster.members[safe_segment(member_name)].get()
        member_data = response.json().get("metadata", {})
        
        # important: LXD reports roles itself, as the list endpoint above already reads them.
        # Deriving them from the `database` flag named every database member the leader,
        # which is how a 3-member MicroCloud read as three leaders on the member page.
        return ClusterMember(
            server_name=member_data.get("server_name", member_name),
            url=member_data.get("url", ""),
            database=member_data.get("database", False),
            status=member_data.get("status", "Online"),
            message=member_data.get("message", "Fully operational"),
            roles=member_data.get("roles", []) or [],
            architecture=member_data.get("architecture"),
            failure_domain=member_data.get("failure_domain", "default"),
            description=member_data.get("description"),
            groups=member_data.get("groups", [])
        )
        
    except HTTPException:
        # broad handler below converts it into a 500
        # important: let require_client's 503 through. Without this the
        # so an offline cluster does not read as a server fault.
        raise
    except Exception as e:
        logger.error(f"Error getting cluster member {member_name} from {node}: {e}")
        raise HTTPException(status_code=404, detail=f"Member '{member_name}' not found")
"""
Additional endpoint for cluster members resources
"""

# A body that could not be read in full is kept this long, not the full lxd data TTL.
DEGRADED_CACHE_TTL = 10


def parse_cpu_limit(limits_cpu: object) -> int:
    """Cores an instance's `limits.cpu` allocates: a count, a range ("0-3") or a list ("0,2")."""
    if limits_cpu is None or limits_cpu == "":
        return 1
    text = str(limits_cpu).strip()
    try:
        if "-" in text:
            low, high = text.split("-", 1)
            return int(high) - int(low) + 1
        if "," in text:
            return len([part for part in text.split(",") if part.strip()])
        return int(text)
    except (TypeError, ValueError):
        return 1


def count_member_instances(instances, member_name: str) -> Tuple[int, int, int]:
    """(instances on this member, of which running, cores those allocate).

    note: "CPU used" here is allocation, the sum of `limits.cpu` of the running instances
    placed on this member (one core for an instance with no limit). LXD's resources API has
    no host utilisation figure and nothing here reads /1.0/metrics, so the UI must label it
    as allocated, not as utilisation.
    """
    count = running = cores = 0
    for instance in instances:
        if getattr(instance, "location", "") != member_name:
            continue
        count += 1
        if getattr(instance, "status", "") == "Running":
            running += 1
            config = getattr(instance, "config", {}) or {}
            cores += parse_cpu_limit(config.get("limits.cpu"))
    return count, running, cores


def _read_member_resources(lxd_service: LXDService, node: str, member_name: str) -> MemberResources:
    """Everything the member dialog shows, read from the cluster. Runs in a worker thread.

    Storage: a storage pool is a cluster-wide object, so `GET /1.0/storage-pools?target=` lists
    every pool for every member and summing them gave each member the cluster's shared ceph
    pools on top of its own disk (local 1.7 TB reported as 12.4 TB). The readings are folded by
    summarise_cluster_storage, the same rule the node card uses: a local pool is this member's,
    a shared pool is the cluster's and is reported once, marked `scope: shared`.
    """
    client = require_client(lxd_service.connection_manager.get_client(node), node)

    # Get resources for this specific member by targeting it
    member_resources_response = client.api.resources.get(params={"target": member_name})
    # important: stamped at the read, then carried through the cache untouched.
    collected_at = datetime.now(timezone.utc)
    member_resources = member_resources_response.json().get("metadata", {}) or {}

    cpu_info = member_resources.get("cpu", {}) or {}
    cpu_total = int(cpu_info.get("total", 0) or 0)

    memory_info = member_resources.get("memory", {}) or {}
    memory_total = int(memory_info.get("total", 0) or 0)
    memory_used = int(memory_info.get("used", 0) or 0)
    memory_available = memory_total - memory_used

    # Storage: one pool list, one space read per pool as this member sees it, then the fold.
    storage_partial = False
    readings: List[PoolReading] = []
    pool_locations: Dict[str, List[str]] = {}
    try:
        pools_response = client.api.storage_pools.get(params={"recursion": 1})
        pool_rows = [r for r in (pools_response.json().get("metadata") or []) if isinstance(r, dict) and r.get("name")]
    except Exception as storage_err:  # noqa: BLE001 - reported as partial, never as zero
        logger.warning(f"Could not list storage pools for member {member_name}: {storage_err}")
        pool_rows = []
        storage_partial = True
    for row in pool_rows:
        pool_locations[row["name"]] = list(row.get("locations") or [])
        reading = _read_pool_space(client, node, member_name, row["name"], row.get("driver") or "unknown")
        if reading is None:
            storage_partial = True
            continue
        readings.append(reading)

    fold = summarise_cluster_storage(readings)
    storage_pools: List[Dict[str, Any]] = []
    storage_local_total = storage_local_used = 0
    storage_shared_total = storage_shared_used = 0
    for pool in fold.pools:
        entry = dict(pool)
        if entry.get("scope") == "shared":
            entry["members"] = pool_locations.get(str(entry["name"]), entry.get("members") or [])
            storage_shared_total += int(entry["space_total"])
            storage_shared_used += int(entry["space_used"])
        else:
            storage_local_total += int(entry["space_total"])
            storage_local_used += int(entry["space_used"])
        storage_pools.append(entry)
    # storage_total keeps its meaning of "the sum of the pool rows below": what this member
    # can place work on, shared pools included. The local and shared subtotals are what make
    # the per-member figures additive across the cluster.
    storage_total = storage_local_total + storage_shared_total
    storage_used = storage_local_used + storage_shared_used
    storage_available = storage_total - storage_used

    # Instances on this member, every project, one request. The same list feeds the GPU
    # inventory, whose cards carry instance names, so the tenant and project filters below
    # decide what this caller may learn.
    cpu_used = 0
    instances_count = 0
    instances_running = 0
    instances = []
    instances_partial = False
    try:
        instances, complete = list_instance_records(client)
        instances_partial = not complete
        acl = get_tenant_acl()
        if acl is not None:
            instances = [
                i for i in instances
                if tenant_acl_has_permission(
                    acl,
                    node,
                    getattr(i, "project", "default") or "default",
                    getattr(i, "name", None),
                    "view",
                )
            ]
        # A Partner is scoped by cluster, not by project, so it needs the project bound too.
        instances = [
            i for i in instances
            if project_is_visible(node, getattr(i, "project", "default") or "default")
        ]
        instances_count, instances_running, cpu_used = count_member_instances(instances, member_name)
    except Exception as usage_err:  # noqa: BLE001
        # important: a failed read here is not "no instances". Reporting zero would show
        # a busy member as empty capacity, so the counts are marked as a floor and the
        # dialog renders them as unavailable.
        logger.warning(f"Could not calculate usage for member {member_name}: {usage_err}")
        instances = []
        instances_partial = True

    # GPU capacity, allocation and card detail from the canonical inventory, scoped to
    # this member. Counting cards here by hand is what made this endpoint report two GPUs
    # on a Jetson while the rest of the product reported one.
    gpu_inv = build_gpu_inventory(member_resources, instances, member=member_name)

    cpu_usage_percent = round((cpu_used / cpu_total * 100), 2) if cpu_total > 0 else 0.0
    memory_usage_percent = round((memory_used / memory_total * 100), 2) if memory_total > 0 else 0.0
    storage_usage_percent = round((storage_used / storage_total * 100), 2) if storage_total > 0 else 0.0

    return MemberResources(
        member_name=member_name,
        cpu_total=cpu_total,
        cpu_used=cpu_used,
        cpu_available=cpu_total - cpu_used,
        cpu_usage_percent=cpu_usage_percent,
        memory_total=memory_total,
        memory_used=memory_used,
        memory_available=memory_available,
        memory_usage_percent=memory_usage_percent,
        gpu_total=gpu_inv.gpu_total,
        gpu_used=gpu_inv.gpu_used,
        gpu_available=gpu_inv.gpu_available,
        gpu_cards=gpu_inv.cards,
        gpu_cards_total=gpu_inv.gpu_cards_total,
        gpu_integrated_total=gpu_inv.gpu_integrated_total,
        gpu_display_total=gpu_inv.gpu_display_total,
        gpu_unknown_total=gpu_inv.gpu_unknown_total,
        gpu_vendors=gpu_inv.vendors,
        gpu_models=gpu_inv.models,
        gpu_utilization_supported=gpu_inv.utilization_supported,
        storage_total=storage_total,
        storage_used=storage_used,
        storage_available=storage_available,
        storage_usage_percent=storage_usage_percent,
        storage_local_total=storage_local_total,
        storage_local_used=storage_local_used,
        storage_shared_total=storage_shared_total,
        storage_shared_used=storage_shared_used,
        storage_partial=storage_partial,
        storage_pools=storage_pools,
        instances_count=instances_count,
        instances_running=instances_running,
        instances_partial=instances_partial,
        collected_at=collected_at,
    )


@router.get(
    "/{member_name}/resources",
    response_model=MemberResources,
    summary="Get member resources",
    description="Get resource information (CPU, RAM, GPU, storage) for a specific cluster member.",
)
@single_flight("member_resources")
async def get_member_resources(
    member_name: str,
    node: str = Query(..., description="Node name to query"),
    lxd_service: LXDService = Depends(get_lxd_service),
    fresh: bool = False,
):
    """Get resource information for a specific cluster member."""
    verify_cluster_access(node)
    cache = get_cache_manager()
    settings = get_settings()
    cache_ttl = settings.cache_ttl_lxd_data
    
    # Check cache first
    cache_key = get_scope_cache_key(f"{MEMBER_RESOURCES_CACHE_PREFIX}:{node}:{member_name}")
    cached_resources = None if fresh else cache.get("lxd_resources", cache_key)
    if cached_resources is not None:
        logger.info(f"Cache HIT for member resources: {node}/{member_name}")
        return MemberResources(**cached_resources)
    
    logger.info(f"Cache MISS for member resources: {node}/{member_name}")

    try:
        # important: every pylxd read runs off the event loop. Inline, a member on a busy
        # MicroCloud parked one worker for the whole read (resources, three pools, the
        # instance list), during which that worker served nothing, /health included.
        # asyncio.to_thread copies the request context, which get_tenant_acl and
        # project_is_visible read from.
        resources = await asyncio.to_thread(_read_member_resources, lxd_service, node, member_name)

        # important: a body that could not be read in full is a floor, not a fact. It is kept
        # only long enough to absorb a burst of identical requests, so the next open of the
        # dialog reads the cluster again instead of showing "0 / 0" for a full minute.
        ttl = DEGRADED_CACHE_TTL if (resources.instances_partial or resources.storage_partial) else cache_ttl
        cache.set("lxd_resources", cache_key, resources.model_dump(), ttl=ttl)
        logger.info(
            f"Member {member_name} resources: CPU={resources.cpu_total} cores ({resources.cpu_used} allocated), "
            f"RAM={resources.memory_total / 1024 / 1024 / 1024:.1f} GB ({resources.memory_used / 1024 / 1024 / 1024:.1f} GB used), "
            f"GPU={resources.gpu_total} compute of {resources.gpu_cards_total} cards "
            f"({resources.gpu_used} used), {resources.instances_running}/{resources.instances_count} instances running"
            f"{' (partial)' if resources.instances_partial else ''}"
        )

        return resources

    except HTTPException:
        # important: let require_client's 503 through, so an offline cluster does not read as a
        # server fault. Without this the broad handler below converts it into a 500.
        raise
    except LXDAPIException as e:
        # important: LXD answers 404 for `?target=<name>` on a host that is not clustered, and
        # for a member name that does not exist. Neither is a server fault, and reporting them
        # as 500 sent an operator looking for a backend bug that was not there.
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status == 404:
            logger.info(f"No cluster member '{member_name}' on {node}: {e}")
            raise HTTPException(
                status_code=404,
                detail=f"'{member_name}' is not a cluster member of '{node}'",
            )
        logger.error(f"Error getting resources for member {member_name} from {node}: {e}")
        raise HTTPException(
            status_code=502,
            detail=f"Cluster '{node}' rejected the member resource request: {str(e)}",
        )
    except Exception as e:
        logger.error(f"Error getting resources for member {member_name} from {node}: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to get resources for member '{member_name}': {str(e)}"
        )
