"""
Cluster member endpoints: the member list, one member's record and one member's resources.
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
from app.core.member_names import member_name_path
from app.services.lxd.node_resources import PoolReading
from app.services.lxd.storage_model import summarise_cluster_storage

logger = logging.getLogger(__name__)

_CLUSTER_MEMBERS_CACHE_KIND = "cluster_members"


def _resource_generation(cache: object, kind: str) -> Optional[str]:
    """Read a resource generation; None from a cache without generations (ex; a test double)."""
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

    A member the cluster reports as Offline never answers `?target=`: the request hangs until
    the pooled read timeout. The member record answers at once and carries the cluster's own
    reason for the operator.
    """
    record = _member_record_status(client, member_name)
    if record and record[0] and record[0].lower() != "online":
        raise MemberUnavailableError(member_name, *record)


def _percent(used: float, total: float) -> float:
    """Usage as a percentage, with a zero total reported as zero rather than dividing."""
    return round((used / total * 100), 2) if total > 0 else 0.0

# important: Partner tier for reads too, matching the pages that use this router
# (useRequireRole(Role.PARTNER)). Org scope still applies on top.
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
    cache = get_cache_manager()
    cache_key = get_scope_cache_key(f"cluster_members:list:host={node}")
    cached_data = None if fresh else cache.get("api_responses", cache_key)
    if cached_data is not None:
        logger.debug(f"Returning cached cluster members for host={node}")
        return ClusterMembersResponse(**cached_data)
    members_generation = _resource_generation(cache, _CLUSTER_MEMBERS_CACHE_KIND)
    
    try:
        # important: an unreachable cluster answers 503. is_clustered=False would render as
        # "Not a Clustered Node" and make an outage read as a configuration fact.
        client = require_client(await client_for(lxd_service, node), node)

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
        
        try:
            # Without recursion=1 LXD returns member URLs instead of full objects.
            response = client.api.cluster.members.get(params={"recursion": 1})
            response_data = response.json()

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
                if not isinstance(member_data, dict):
                    logger.warning(f"Skipping non-dict member data: {member_data}")
                    continue

                # LXD reports roles as a list, ex; ["database-leader", "database"].
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
            
            response = ClusterMembersResponse(
                node=node,
                is_clustered=True,
                cluster_enabled=True,
                members=members,
                total=len(members)
            )
            
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
            # Clustered, but the member list could not be read: answer an empty list.
            return ClusterMembersResponse(
                node=node,
                is_clustered=True,
                cluster_enabled=True,
                members=[],
                total=0
            )
        
    except HTTPException:
        # important: let require_client's 503 through; the broad handler below would turn it
        # into a 500.
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
    member_name: str = Depends(member_name_path),
    node: str = Query(..., description="Registered Cluster identifier to query"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get details for a specific cluster member."""
    verify_cluster_access(node)
    try:
        client = require_client(await client_for(lxd_service, node), node)

        response = client.api.cluster.members[safe_segment(member_name)].get()
        member_data = response.json().get("metadata", {})

        # Roles come from LXD, as in the list above. The `database` flag is set on every
        # database member, so a role derived from it would name each of them leader.
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
        # important: let require_client's 503 through, so an offline cluster does not read as a
        # missing member. The broad handler below would turn it into a 404.
        raise
    except Exception as e:
        logger.error(f"Error getting cluster member {member_name} from {node}: {e}")
        raise HTTPException(status_code=404, detail=f"Member '{member_name}' not found")


@router.get(
    "/{member_name}/resources",
    response_model=MemberResources,
    summary="Get member resources",
    description="Get CPU, Memory, GPU, and Storage information for a specific Cluster member.",
)
@single_flight("member_resources")
async def get_member_resources(
    member_name: str = Depends(member_name_path),
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

    cache_key = get_scope_cache_key(f"{MEMBER_RESOURCES_CACHE_PREFIX}:{node}:{member_name}")
    cached_resources = None if fresh else cache.get("lxd_resources", cache_key)
    if cached_resources is not None:
        logger.info(f"Cache HIT for member resources: {node}/{member_name}")
        return MemberResources(**cached_resources)
    capacity_generation = _resource_generation(cache, CAPACITY_GENERATION_KIND)

    logger.info(f"Cache MISS for member resources: {node}/{member_name}")

    try:
        # get_client revalidates the pooled connection with a live request and may rebuild it
        # through a TLS handshake, so it runs off the event loop.
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

        # important: the instance list spans every project and feeds both the counts and
        # build_gpu_inventory, whose cards carry `used_by` instance names. Filter it by any tenant
        # ACL in effect, so another organization's instance names never reach the member panel.
        # The captured ACL, not the contextvar, keeps the rule the same on any thread.
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

        # A storage pool is cluster-wide, so the member's pool list carries the shared ceph pools
        # on top of its own disks. summarise_cluster_storage folds them by the node card's rule:
        # a local pool is this member's; a shared pool is the cluster's, reported once and marked
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
        # storage_total is the sum of the pool rows: what this member can place work on, shared
        # pools included. The local and shared subtotals make per-member figures additive
        # across the cluster.
        storage_total = storage_local_total + storage_shared_total
        storage_used = storage_local_used + storage_shared_used
        storage_partial = any(part.startswith(PART_STORAGE) for part in fanout.degraded_parts)
        instances_partial = PART_INSTANCES in fanout.degraded_parts

        # GPU capacity, allocation and card detail come from the shared inventory, scoped to
        # this member, so every endpoint counts cards the same way.
        gpu_inv = build_gpu_inventory(
            fanout.resources_body, fanout.instances, member=member_name
        )

        resources = MemberResources(
            member_name=member_name,
            cpu_total=cpu_total,
            cpu_used=usage.cpu_used,
            # Allocation, not utilisation, so it may exceed the thread count under KVM
            # over-commit (ex; 307 vCPUs on 80 threads). The remainder is clamped at zero and
            # the UI presents the ratio.
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

        # important: a partial read gets a much shorter TTL. Its storage or instance numbers are
        # floors, and a full TTL would keep serving them after the transient failure has passed.
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
        # important: let require_client's 503 through with its own detail; the broad handler
        # below would replace it with a generic one.
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
        # LXD answers 404 for `?target=<name>` on a host that is not clustered and for a member
        # name that does not exist. Neither is a server fault.
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
        # important: a read timeout arrives as ConnectionError, not ReadTimeout, because the
        # pooled adapter runs Retry(read=0); lxd_errors.read_timed_out tells the two apart.
        logger.error(f"Transport failure reading resources for {member_name} from {node}: {e}")
        raise lxd_transport_error(e, node, f"reading resources for '{member_name}'") from None
    except Exception as e:
        logger.error(f"Error getting resources for member {member_name} from {node}: {e}")
        raise HTTPException(
            status_code=503,
            detail=f"Could not get resources for member '{member_name}': {str(e)}"
        )
