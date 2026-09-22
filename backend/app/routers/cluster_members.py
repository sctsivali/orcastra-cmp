"""
API Router for MicroCloud cluster members.
Provides endpoints for listing cluster members in a MicroCloud setup.
"""

from concurrent.futures import ThreadPoolExecutor
from fastapi import APIRouter, HTTPException, Depends, Query
from typing import List, Optional, Tuple
import logging

import requests.exceptions as requests_exc

from app.services.lxd_service import (
    LXDService,
    get_lxd_service,
    require_client,
)
from pydantic import BaseModel, Field
from pylxd.exceptions import LXDAPIException
from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking
from app.core.lxd_errors import lxd_transport_error
from app.core.single_flight import single_flight
from app.core.cache import (
    CAPACITY_GENERATION_KIND,
    MEMBER_RESOURCES_CACHE_PREFIX,
    get_cache_manager,
)
from app.core.config import get_settings
from app.models.schemas import (
    MemberResources, 
)

from app.services.lxd.gpu import build_gpu_inventory
from app.services.lxd.cpu_allocation import allocation_ratio, clamp_available
from app.services.lxd.member_resources import (
    PART_INSTANCES,
    PART_STORAGE,
    collect_member_fanout,
    count_member_instances,
)
from app.core.dependencies import require_partner
from app.core.org_scope import (
    org_scope_dependency,
    verify_cluster_access,
    get_scope_cache_key,
    get_tenant_acl,
    project_is_visible,
)
from app.core.tenant_visibility import tenant_acl_has_permission
from app.services.lxd.lazy_client import client_for
from app.core.lxd_path import safe_segment
from app.services.lxd.node_resources import PoolReading
from app.services.lxd.storage_model import summarise_cluster_storage

logger = logging.getLogger(__name__)

_CLUSTER_MEMBERS_CACHE_KIND = "cluster_members"


def _resource_generation(cache: object, kind: str) -> Optional[str]:
    """Read a resource generation, allowing deliberately minimal cache test doubles."""
    read = getattr(cache, "resource_list_generation", None)
    return read(kind) if callable(read) else None

_connect_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="member_connect")
# get_client revalidates with a live (3, 5) GET and may rebuild the client, which costs a TLS
# handshake plus a verifying (2, 5) GET. Matches the recovery-connect budget in image_uploads.
MEMBER_CONNECT_TIMEOUT: float = 30.0
# A body carrying floors instead of measurements must not sit in the cache for a full minute.
DEGRADED_CACHE_TTL: int = 10


class MemberUnavailableError(Exception):
    """The cluster's own record says this member is not Online, so it will not answer a read."""

    def __init__(self, member_name: str, status: str, message: str) -> None:
        super().__init__(f"Member '{member_name}' is {status}" + (f": {message}" if message else ""))
        self.member_name, self.status, self.message = member_name, status, message


def _member_record_status(client, member_name: str) -> Optional[Tuple[str, str]]:
    """(status, message) as the cluster's member record has it, or None when it cannot be read."""
    try:
        record = client.api.cluster.members[safe_segment(member_name)].get().json().get("metadata") or {}
    except Exception:
        return None
    return (str(record.get("status") or ""), str(record.get("message") or ""))


def _refuse_offline_member(client, member_name: str) -> None:
    """Refuse at once when the cluster reports the member as not Online.

    important: a member the cluster itself reports as Offline never answers `?target=`: the
    request hangs on the cluster until the pooled read timeout, and the dialog waited through
    that and a retry for a generic failure. The member record answers at once, so ask it
    first and let the operator read the cluster's own reason.
    """
    record = _member_record_status(client, member_name)
    if record and record[0] and record[0].lower() != "online":
        raise MemberUnavailableError(member_name, *record)


def _percent(used: float, total: float) -> float:
    """Usage as a percentage, with a zero total reported as zero rather than dividing."""
    return round((used / total * 100), 2) if total > 0 else 0.0

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
    node: str = Field(..., description="Registered Cluster identifier being queried")
    is_clustered: bool = Field(..., description="Whether the registered endpoint has clustering enabled")
    cluster_enabled: bool = Field(..., description="Whether clustering is enabled")
    members: List[ClusterMember] = Field(default_factory=list, description="List of cluster members")
    total: int = Field(0, description="Total number of members")


@router.get(
    "",
    response_model=ClusterMembersResponse,
    summary="List cluster members",
    description="Get all machines in a hypervisor cluster. Only works for Clusters with clustering enabled.",
)
async def list_cluster_members(
    node: str = Query(..., description="Registered Cluster identifier to query"),
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
    members_generation = _resource_generation(cache, _CLUSTER_MEMBERS_CACHE_KIND)
    
    try:
        # important: an unreachable cluster answers 503 here like everywhere else. This
        # used to return is_clustered=False, and the members page renders that as
        # "Not a Clustered Node", so an outage read as a configuration fact. The sibling
        # get_member_resources has always used require_client.
        client = require_client(await client_for(lxd_service, node), node)

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
            
            # Cache as dict for 180 seconds
            if (
                _resource_generation(cache, _CLUSTER_MEMBERS_CACHE_KIND)
                == members_generation
            ):
                cache.set("api_responses", cache_key, response.model_dump(), ttl=180)
                logger.debug("Cached cluster members for 180 seconds")
            else:
                logger.info("Not caching cluster members: membership changed during the read")
            
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
    node: str = Query(..., description="Registered Cluster identifier to query"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get details for a specific cluster member."""
    verify_cluster_access(node)
    try:
        client = require_client(await client_for(lxd_service, node), node)
        
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

@router.get(
    "/{member_name}/resources",
    response_model=MemberResources,
    summary="Get member resources",
    description="Get CPU, Memory, GPU, and Storage information for a specific Cluster member.",
)
@single_flight("member_resources")
async def get_member_resources(
    member_name: str,
    node: str = Query(..., description="Registered Cluster identifier to query"),
    lxd_service: LXDService = Depends(get_lxd_service),
    fresh: bool = False,
):
    """Get resource information for a specific cluster member.

    The fan-out itself lives in services/lxd/member_resources.py. This keeps the HTTP concerns:
    scope, cache, the connection hop, and turning a failure into a status an operator can act on.
    """
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
    capacity_generation = _resource_generation(cache, CAPACITY_GENERATION_KIND)

    logger.info(f"Cache MISS for member resources: {node}/{member_name}")

    try:
        # note: get_client revalidates the pooled connection with a live request and may rebuild
        # it through a TLS handshake, so it is blocking work and does not belong on the loop.
        raw_client = await run_blocking(
            lxd_service.connection_manager.get_client,
            node,
            timeout=MEMBER_CONNECT_TIMEOUT,
            label=f"member_resources_connect[{node}]",
            executor=_connect_executor,
        )
        client = require_client(raw_client, node)

        await run_blocking(
            _refuse_offline_member,
            client,
            member_name,
            timeout=MEMBER_CONNECT_TIMEOUT,
            label=f"member_record[{node}/{member_name}]",
            executor=_connect_executor,
        )

        fanout = await collect_member_fanout(client, node, member_name)

        cpu_info = fanout.resources_body.get("cpu", {}) or {}
        cpu_total = cpu_info.get("total", 0) or 0
        memory_info = fanout.resources_body.get("memory", {}) or {}
        memory_total = memory_info.get("total", 0) or 0
        memory_used = memory_info.get("used", 0) or 0

        # important: the instance list now spans every project, and it feeds both the counts
        # and build_gpu_inventory, whose cards carry `used_by` instance NAMES. This router is
        # reachable by a Tenant, so without a filter the widening publishes another
        # organization's instance names on the member panel. The captured ACL is used rather
        # than the contextvar, so the rule is the same whichever thread reads it.
        acl = get_tenant_acl()
        if acl is not None:
            fanout.instances = [
                i for i in fanout.instances
                if tenant_acl_has_permission(
                    acl, node,
                    getattr(i, "project", "default") or "default",
                    getattr(i, "name", None), "view",
                )
            ]
        # A Partner is scoped by cluster, not by project, so it needs the project bound too.
        fanout.instances = [
            i for i in fanout.instances
            if project_is_visible(node, getattr(i, "project", "default") or "default")
        ]

        usage = count_member_instances(fanout.instances, member_name)

        # Storage: a storage pool is a cluster-wide object, so the member's pool list carries
        # the cluster's shared ceph pools on top of its own disks, and summing them gave each
        # member the whole cluster's storage (local 1.7 TB reported as 12.4 TB). The readings
        # are folded by summarise_cluster_storage, the same rule the node card uses: a local
        # pool is this member's, a shared pool is the cluster's and is reported once, marked
        # `scope: shared` with the members it is defined on.
        fold = summarise_cluster_storage(
            PoolReading(
                member=member_name,
                name=pool.name,
                driver=pool.driver,
                space_total=pool.space_total,
                space_used=pool.space_used,
            )
            for pool in fanout.pools
        )
        pool_members = {pool.name: list(pool.members) for pool in fanout.pools}
        storage_pools = []
        storage_local_total = storage_local_used = 0
        storage_shared_total = storage_shared_used = 0
        for pool in fold.pools:
            entry = dict(pool)
            if entry.get("scope") == "shared":
                entry["members"] = pool_members.get(str(entry["name"])) or entry.get("members") or []
                storage_shared_total += int(entry["space_total"])
                storage_shared_used += int(entry["space_used"])
            else:
                storage_local_total += int(entry["space_total"])
                storage_local_used += int(entry["space_used"])
            storage_pools.append(entry)
        # storage_total keeps its meaning of "the sum of the pool rows below": what this member
        # can place work on, shared pools included. The local and shared subtotals are what
        # make the per-member figures additive across the cluster.
        storage_total = storage_local_total + storage_shared_total
        storage_used = storage_local_used + storage_shared_used
        storage_partial = any(part.startswith(PART_STORAGE) for part in fanout.degraded_parts)
        instances_partial = PART_INSTANCES in fanout.degraded_parts

        # GPU capacity, allocation and card detail from the canonical inventory, scoped to
        # this member. Counting cards here by hand is what made this endpoint report two GPUs
        # on a Jetson while the rest of the product reported one.
        gpu_inv = build_gpu_inventory(
            fanout.resources_body, fanout.instances, member=member_name
        )

        resources = MemberResources(
            member_name=member_name,
            cpu_total=cpu_total,
            cpu_used=usage.cpu_used,
            # Allocation, not utilisation, so it may exceed the thread count: 307 vCPUs on 80
            # threads is a legitimate KVM over-commit. The remainder is clamped and the ratio
            # is what the UI presents; a negative "available" and a 383 % bar were the bug.
            cpu_available=clamp_available(cpu_total, usage.cpu_used),
            cpu_usage_percent=_percent(usage.cpu_used, cpu_total),
            cpu_allocated=usage.cpu_used,
            cpu_allocation_ratio=allocation_ratio(usage.cpu_used, cpu_total),
            cpu_overcommitted=usage.cpu_used > cpu_total,
            memory_total=memory_total,
            memory_used=memory_used,
            memory_available=memory_total - memory_used,
            memory_usage_percent=_percent(memory_used, memory_total),
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
            storage_available=storage_total - storage_used,
            storage_usage_percent=_percent(storage_used, storage_total),
            storage_local_total=storage_local_total,
            storage_local_used=storage_local_used,
            storage_shared_total=storage_shared_total,
            storage_shared_used=storage_shared_used,
            storage_partial=storage_partial,
            storage_pools=storage_pools,
            instances_count=usage.count,
            instances_running=usage.running,
            instances_partial=instances_partial,
            collected_at=fanout.collected_at,
            degraded=fanout.degraded,
            degraded_parts=fanout.degraded_parts,
        )

        # important: a partial read gets a much shorter TTL. Caching a body whose storage or
        # instance numbers are floors for the full minute would keep serving them long after
        # the transient that caused it, and the operator has no way to ask for a rebuild that
        # the cache would not answer.
        if _resource_generation(cache, CAPACITY_GENERATION_KIND) == capacity_generation:
            cache.set(
                "lxd_resources",
                cache_key,
                resources.model_dump(),
                ttl=DEGRADED_CACHE_TTL if fanout.degraded else cache_ttl,
            )
        else:
            logger.info("Not caching member resources: capacity changed during the read")
        logger.info(
            f"Member {member_name} resources: CPU capacity={cpu_total} hardware threads "
            f"({usage.cpu_used} vCPUs allocated), "
            f"Memory={memory_total / 1024 / 1024 / 1024:.1f} GiB "
            f"({memory_used / 1024 / 1024 / 1024:.1f} GiB used), "
            f"GPU={gpu_inv.gpu_total} compute of {gpu_inv.gpu_cards_total} cards "
            f"({gpu_inv.gpu_used} used), {usage.running}/{usage.count} instances running"
            + (f", degraded: {','.join(fanout.degraded_parts)}" if fanout.degraded else "")
        )

        return resources

    except HTTPException:
        # important: let require_client's 503 through, so an offline cluster does not read as a
        # server fault. Without this the broad handler below converts it into a 500.
        raise
    except MemberUnavailableError as e:
        logger.info(f"Member {member_name} on {node} is {e.status}: {e.message}")
        raise HTTPException(status_code=503, detail=str(e)) from None
    except BlockingTimeout:
        raise HTTPException(
            status_code=504,
            detail=f"Reading resources for '{member_name}' on {node} timed out",
        ) from None
    except BlockingPoolFull:
        raise HTTPException(
            status_code=503,
            detail="Cluster reads are saturated right now, please retry shortly",
        ) from None
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
    except (requests_exc.ConnectionError, requests_exc.Timeout) as e:
        # important: a read timeout reaches here as ConnectionError, not ReadTimeout, because
        # the pooled adapter runs Retry(read=0). lxd_errors.read_timed_out carries the proof.
        logger.error(f"Transport failure reading resources for {member_name} from {node}: {e}")
        raise lxd_transport_error(e, node, f"reading resources for '{member_name}'") from None
    except Exception as e:
        logger.error(f"Error getting resources for member {member_name} from {node}: {e}")
        raise HTTPException(
            status_code=503,
            detail=f"Failed to get resources for member '{member_name}': {str(e)}"
        )
