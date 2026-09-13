"""
API Router for IPAM (IP Address Management) operations.
Provides endpoints for managing LXD network IP address allocations.
"""

from fastapi import APIRouter, HTTPException, Depends, Query
from typing import Any, Dict, Literal, Optional, List, Tuple
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor

from app.services.lxd.exceptions import LXDConnectionError
from app.services.lxd.fanout import NodeRead, collect, unattempted
from app.services.lxd.ipam_join import classify, network_facts
from app.services.lxd.project_scope import (
    InstanceRecord,
    list_across_projects,
    list_instance_records,
    list_project_configs,
)
from app.services.lxd_service import (
    LXDService,
    get_lxd_service,
)
from pydantic import BaseModel, Field
from app.models.schemas import FanoutReachability
from app.services.lxd.proxy import socket_timeout_for
from app.core.dependencies import require_tenant
from app.core.org_scope import (
    org_scope_dependency,
    get_scope_cache_key,
    verify_cluster_access,
    check_tenant_permission,
    project_is_visible,
)
from app.core.tenant_visibility import tenant_acl_active
from app.core.single_flight import single_flight
from app.core.cache import get_cache_manager

logger = logging.getLogger(__name__)

# thread pool for parallel node queries
_ipam_executor = ThreadPoolExecutor(max_workers=10)

router = APIRouter(
    prefix="/ipam",
    tags=["IPAM"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)


AllocationKind = Literal["static", "dhcp", "gateway", "forward", "load_balancer", "unknown"]

# the cache key carries a version: a body cached before the join fields existed must not be
# served as "unknown" for the rest of its TTL after a deploy
_CACHE_VERSION = "v2"
# a known-down cluster is a correct answer, remembered briefly so recovery shows within the
# healer's cadence (same rule as storage_pools.py)
DEGRADED_CACHE_TTL = 60


class IPAMAllocation(BaseModel):
    """IPAM allocation information."""
    address: str = Field(..., description="IP address")
    type: str = Field(..., description="Address type (instance, network, etc)")
    used_by: str = Field(..., description="Resource using this address")
    hardware_address: str = Field("", description="MAC address")
    network: str = Field(..., description="Network name")
    node: str = Field(..., description="Node name")
    project: str = Field("default", description="Project name")
    allocation: AllocationKind = Field(
        "unknown",
        description=(
            "static when the instance's NIC on this network pins the address on the device "
            "(its own or inherited from a profile); dhcp when the instance holds it without a "
            "pin, which covers dnsmasq leases, OVN dynamic addresses and SLAAC; gateway, "
            "forward and load_balancer for the network's own rows; unknown when the instances "
            "of this node could not be read. A stale lease beside a fresh pin shows as one "
            "static and one dhcp row for the same instance, which is the truthful picture."
        ),
    )
    network_type: str = Field("", description="LXD network type (bridge, ovn, macvlan, physical); empty when the network was not found")
    network_project: str = Field("", description="The project that holds the network record; empty when the network was not found")
    subnet_ipv4: str = Field("", description="The network's ipv4.address CIDR, empty when none")
    subnet_ipv6: str = Field("", description="The network's ipv6.address CIDR, empty when none")


class IPAMListResponse(FanoutReachability):
    """Response for listing IPAM allocations."""
    total: int = Field(..., description="Total number of allocations")
    allocations: List[IPAMAllocation] = Field(..., description="List of IP allocations")
    nodes: List[str] = Field(..., description="List of nodes")


def _generation(cache: Any) -> Optional[str]:
    """The instance-list generation token, or None from a cache that keeps none (a stub)."""
    read = getattr(cache, "instance_list_generation", None)
    return read() if callable(read) else None


def _read_join_sources(
    client: Any, node_name: str, read: NodeRead
) -> Tuple[Dict[str, Dict[str, str]], Dict[Tuple[str, str], Dict[str, Any]], Dict[Tuple[str, str], InstanceRecord], bool]:
    """The projects, networks and instances of one cluster, each read once for the whole node.

    A failed read is named on the NodeRead and degrades the join rather than the listing: no
    networks means empty network facts, no instances means every instance row is "unknown".
    """
    try:
        configs = list_project_configs(client)
    except Exception as e:  # noqa: BLE001 - named, not fatal
        read.lost(f"projects: {type(e).__name__}")
        configs = {"default": {}}
    networks: Dict[Tuple[str, str], Dict[str, Any]] = {}
    try:
        rows, complete = list_across_projects(client, "networks")
        networks = {(str(r.get("project") or "default"), str(r.get("name") or "")): r for r in rows}
        if not complete:
            read.lost("networks: partial read")
    except Exception as e:  # noqa: BLE001
        read.lost(f"networks: {type(e).__name__}")
    records: Dict[Tuple[str, str], InstanceRecord] = {}
    instances_ok = True
    try:
        instance_rows, complete = list_instance_records(client)
        records = {(rec.project, rec.name): rec for rec in instance_rows}
        if not complete:
            read.lost("instances: partial read")
            instances_ok = False
    except Exception as e:  # noqa: BLE001
        read.lost(f"instances: {type(e).__name__}")
        instances_ok = False
    return configs, networks, records, instances_ok


def _fetch_ipam_for_node(
    lxd_service: LXDService,
    node_name: str,
    network_filter: Optional[str],
) -> NodeRead[IPAMAllocation]:
    """Fetch IPAM allocations for a single node (runs in thread pool)."""
    read: NodeRead[IPAMAllocation] = NodeRead()
    client = lxd_service.connection_manager.get_client(node_name)
    if client is None:
        # important: unreachable is not empty. Returning a bare collection
        # here leaves the caller nothing to label, which is how a dead
        # cluster read as a cluster with nothing.
        raise LXDConnectionError(f"Could not connect to {node_name}")

    configs, networks, records, instances_ok = _read_join_sources(client, node_name, read)
    project_names = list(configs.keys()) or ["default"]

    for project_name in project_names:
        try:
            base_url = client.api._api_endpoint
            allocations_url = f"network-allocations?project={project_name}&recursion=1"
            full_url = f"{base_url}/{allocations_url}"
            # important: client.api.session bypasses pylxd's _APINode, so the timeout
            # _APINode would have injected never applies. Without this the call blocks
            # forever against a silent node and permanently burns a pool thread.
            allocations_response = client.api.session.get(
                full_url, timeout=socket_timeout_for(client)
            )

            if allocations_response.status_code != 200:
                logger.debug(
                    f"Could not get network allocations for project {project_name} on {node_name}: "
                    f"status {allocations_response.status_code}"
                )
                continue
            for alloc in allocations_response.json().get('metadata', []):
                alloc_network = alloc.get('network', '')
                if network_filter and alloc_network != network_filter:
                    continue

                addresses_str = alloc.get('addresses', '')
                address = addresses_str.split('/')[0] if addresses_str else ''
                alloc_type = alloc.get('type', 'network')
                used_by = alloc.get('used_by', '')
                hwaddr = alloc.get('hwaddr', '')

                if used_by and '/instances/' in used_by:
                    resource_type = "instance"
                    resource_name = used_by.split('/instances/')[-1].split('?')[0]
                elif used_by:
                    resource_type = alloc_type
                    resource_name = alloc_network
                else:
                    resource_type = "network"
                    resource_name = alloc_network
                if not address:
                    continue

                # the row's project is the instance's project: LXD lists allocations per project
                record = records.get((project_name, resource_name)) if resource_type == "instance" else None
                kind = classify(
                    alloc_type if resource_type != "instance" else "instance",
                    address, record, alloc_network, instances_ok,
                )
                net_type, net_project, subnet4, subnet6 = network_facts(
                    alloc_network, project_name, configs, networks
                )
                read.rows.append(IPAMAllocation(
                    address=address,
                    type=resource_type,
                    used_by=resource_name,
                    hardware_address=hwaddr or "",
                    network=alloc_network,
                    node=node_name,
                    project=project_name,
                    allocation=kind,
                    network_type=net_type,
                    network_project=net_project,
                    subnet_ipv4=subnet4,
                    subnet_ipv6=subnet6,
                ))
        except Exception as e:  # noqa: BLE001 - one project's failure is named, not fatal
            # the project is in the log for the operator of the box; the banner a Partner on a
            # shared cluster reads must not name another organization's project
            logger.debug(f"Error getting allocations for project {project_name} on {node_name}: {e}")
            read.lost(f"one project's allocations: {type(e).__name__}")
    if read.problems:
        logger.warning("IPAM read on %s lost %d part(s): %s", node_name, len(read.problems), "; ".join(read.problems))
    return read


@router.get(
    "",
    response_model=IPAMListResponse,
    summary="List IPAM allocations",
    description="Get a list of all IP address allocations across networks.",
)
@single_flight("ipam:list")
async def list_ipam_allocations(
    host: Optional[str] = Query(None, description="Filter by specific host node"),
    network: Optional[str] = Query(None, description="Filter by specific network"),
    lxd_service: LXDService = Depends(get_lxd_service),
    fresh: bool = False,
):
    """List all IPAM allocations. Nodes are queried in parallel with caching."""
    if host:
        verify_cluster_access(host)
    # check cache first (120s TTL)
    cache = get_cache_manager()
    cache_key = get_scope_cache_key(f"ipam:list:{_CACHE_VERSION}:host={host}:network={network}")
    cached = None if fresh else cache.get("api_responses", cache_key)
    if cached is not None:
        logger.debug(f"Returning cached IPAM data for {cache_key}")
        return IPAMListResponse(**cached)

    try:
        # important: a NIC pin or an instance lifecycle change bumps this while a fan-out is in
        # flight; the read then holds the old allocation table and must not be written back.
        generation = _generation(cache)
        nodes_to_query = [host] if host else lxd_service.connection_manager.get_node_names()

        # query all nodes in parallel
        loop = asyncio.get_event_loop()
        tasks = [
            loop.run_in_executor(_ipam_executor, _fetch_ipam_for_node, lxd_service, node_name, network)
            for node_name in nodes_to_query
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # important: two different things make a node absent, and both must be named.
        # A node can fail during the fan-out, or never be asked at all, because
        # connection_manager.get_node_names() returns only nodes with a live client while
        # the fleet is whatever Vault has registered. Reporting only the first kind left a
        # dead cluster silently missing rather than merely empty.
        seeded: List[str] = (
            [] if host else sorted(set(lxd_service.get_node_names()) - set(nodes_to_query))
        )
        unreachable_nodes: List[str] = list(seeded)
        fan = collect(
            nodes_to_query, results, noun="IP allocations", reasons_visible=not tenant_acl_active()
        )
        unreachable_nodes.extend(fan.unreachable_nodes)
        cacheable = fan.cacheable and not unattempted(seeded, lxd_service.connection_manager)
        ttl = DEGRADED_CACHE_TTL if unreachable_nodes else 120

        all_allocations = [
            allocation for allocation in fan.rows
            if project_is_visible(allocation.node, allocation.project or "default")
            and check_tenant_permission(
                allocation.node,
                allocation.project or "default",
                allocation.used_by if allocation.type == "instance" else None,
                "view",
            )
        ]

        response = IPAMListResponse(
            total=len(all_allocations),
            allocations=all_allocations,
            nodes=lxd_service.connection_manager.get_node_names(),
            unreachable_nodes=unreachable_nodes,
            partial_nodes=fan.partial_nodes,
            node_errors=fan.node_errors,
        )

        # important: an outage must not be remembered as the answer, and neither may a partial
        # one or a read that an invalidation overtook. See instances.py and storage_pools.py.
        if cacheable:
            if _generation(cache) != generation:
                logger.info("Not caching IPAM list: an instance changed during the read")
            else:
                cache.set("api_responses", cache_key, response.model_dump(), ttl=ttl)
        else:
            logger.warning("Not caching IPAM list: %s", fan.node_errors or "no node answered")
        return response
    except Exception as e:
        logger.error(f"Error listing IPAM: {e}")
        raise HTTPException(status_code=500, detail=str(e))
