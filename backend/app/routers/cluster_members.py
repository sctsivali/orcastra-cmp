"""
API Router for MicroCloud cluster members.
Provides endpoints for listing cluster members in a MicroCloud setup.
"""

from fastapi import APIRouter, HTTPException, Depends, Query
from typing import List, Optional
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
from app.core.dependencies import require_partner
from app.core.org_scope import org_scope_dependency, verify_cluster_access, get_scope_cache_key

logger = logging.getLogger(__name__)

# important: raised from require_tenant() to require_partner(). Every page that consumes this
# router declares useRequireRole(Role.PARTNER), so the UI already treated it as partner-tier while
# the API accepted a tenant. Mutations here were gated individually and reads were not, which is
# the pattern this closes. Org scope still applies on top, so this narrows the tier rather than
# replacing the boundary.
router = APIRouter(
    prefix="/cluster-members",
    tags=["Cluster Members"],
    dependencies=[Depends(require_partner()), Depends(org_scope_dependency)],
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
        response = client.api.cluster.members[member_name].get()
        member_data = response.json().get("metadata", {})
        
        # Parse roles
        roles = []
        if member_data.get("database", False):
            roles.append("database-leader" if member_data.get("database") else "database")
        
        return ClusterMember(
            server_name=member_data.get("server_name", member_name),
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
        client = require_client(lxd_service.connection_manager.get_client(node), node)
        
        # Get resources for this specific member by targeting it
        member_resources_response = client.api.resources.get(params={"target": member_name})
        # important: stamped at the read, then carried through the 60s cache untouched.
        collected_at = datetime.now(timezone.utc)
        member_resources = member_resources_response.json().get("metadata", {})
        
        # Extract CPU resources
        cpu_info = member_resources.get("cpu", {})
        cpu_total = cpu_info.get("total", 0)
        
        # Extract memory resources
        memory_info = member_resources.get("memory", {})
        memory_total = memory_info.get("total", 0)
        memory_used = memory_info.get("used", 0)
        memory_available = memory_total - memory_used
        
        
        # Get storage pools from separate endpoint
        storage_total = 0
        storage_used = 0
        storage_available = 0
        storage_pools = []
        
        try:
            # Query storage pools for this specific member
            logger.debug(f"Querying storage pools for member {member_name}")
            pools_response = client.api["storage-pools"].get(params={"target": member_name})
            pool_paths = pools_response.json().get("metadata", [])
            # Extract pool names from paths like /1.0/storage-pools/local -> local
            pool_names = [p.split("/")[-1] for p in pool_paths if p]
            logger.info(f"Found {len(pool_names)} storage pools for member {member_name}: {pool_names}")
            
            for pool_name in pool_names:
                try:
                    # Get detailed info for each pool
                    pool_response = client.api["storage-pools"][pool_name].get(params={"target": member_name})
                    pool_data = pool_response.json().get("metadata", {})
                    
                    _pool_config = pool_data.get("config", {})
                    pool_driver = pool_data.get("driver", "unknown")
                    
                    # Get pool resources
                    try:
                        pool_resources = client.api["storage-pools"][pool_name]["resources"].get(params={"target": member_name})
                        resources_data = pool_resources.json().get("metadata", {})
                        space = resources_data.get("space", {})
                        space_total = space.get("total", 0)
                        space_used = space.get("used", 0)
                        logger.debug(f"Pool {pool_name}: {space_total} total, {space_used} used")
                    except Exception as res_err:
                        # Fallback if resources endpoint not available
                        logger.warning(f"Could not get resources for pool {pool_name}: {res_err}")
                        space_total = 0
                        space_used = 0
                    
                    space_available = space_total - space_used
                    
                    storage_total += space_total
                    storage_used += space_used
                    storage_available += space_available
                    
                    storage_pools.append({
                        "name": pool_name,
                        "driver": pool_driver,
                        "space_total": space_total,
                        "space_used": space_used,
                        "space_available": space_available,
                    })
                except Exception as pool_err:
                    logger.warning(f"Could not get details for pool {pool_name}: {pool_err}")
                    continue
        except Exception as storage_err:
            logger.warning(f"Could not get storage pools for member {member_name}: {storage_err}")
        
        # Get CPU and GPU usage by counting instances on this member
        cpu_used = 0
        instances_count = 0
        instances_running = 0
        instances = []

        try:
            instances = client.instances.all()
            for instance in instances:
                instance_location = getattr(instance, 'location', '')
                if instance_location == member_name:
                    instances_count += 1
                    if instance.status == "Running":
                        instances_running += 1
                        
                        # Count CPU
                        config = getattr(instance, 'config', {}) or {}
                        limits_cpu = config.get('limits.cpu')
                        if limits_cpu:
                            try:
                                if '-' in str(limits_cpu):
                                    # Range like "0-3"
                                    parts = str(limits_cpu).split('-')
                                    cpu_used += int(parts[1]) - int(parts[0]) + 1
                                elif ',' in str(limits_cpu):
                                    # List like "0,1,2"
                                    cpu_used += len(str(limits_cpu).split(','))
                                else:
                                    # Single number
                                    cpu_used += int(limits_cpu)
                            except:
                                cpu_used += 1
                        else:
                            cpu_used += 1
                        
        except Exception as usage_err:
            logger.debug(f"Could not calculate usage for member {member_name}: {usage_err}")
        
        # GPU capacity, allocation and card detail from the canonical inventory, scoped to
        # this member. Counting cards here by hand is what made this endpoint report two GPUs
        # on a Jetson while the rest of the product reported one.
        gpu_inv = build_gpu_inventory(member_resources, instances, member=member_name)
        
        # Calculate percentages
        cpu_usage_percent = round((cpu_used / cpu_total * 100), 2) if cpu_total > 0 else 0.0
        memory_usage_percent = round((memory_used / memory_total * 100), 2) if memory_total > 0 else 0.0
        storage_usage_percent = round((storage_used / storage_total * 100), 2) if storage_total > 0 else 0.0
        
        # Create response
        resources = MemberResources(
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
            storage_pools=storage_pools,
            instances_count=instances_count,
            instances_running=instances_running,
            collected_at=collected_at,
        )
        
        # Cache the result
        cache.set("lxd_resources", cache_key, resources.model_dump(), ttl=cache_ttl)
        logger.info(
            f"Member {member_name} resources: CPU={cpu_total} cores ({cpu_used} used), "
            f"RAM={memory_total / 1024 / 1024 / 1024:.1f} GB ({memory_used / 1024 / 1024 / 1024:.1f} GB used), "
            f"GPU={gpu_inv.gpu_total} compute of {gpu_inv.gpu_cards_total} cards "
            f"({gpu_inv.gpu_used} used), {instances_running}/{instances_count} instances running"
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
