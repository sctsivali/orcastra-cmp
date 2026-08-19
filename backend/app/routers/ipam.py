"""
API Router for IPAM (IP Address Management) operations.
Provides endpoints for managing LXD network IP address allocations.
"""

from fastapi import APIRouter, HTTPException, Depends, Query
from typing import Optional, List
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor

from app.services.lxd.exceptions import LXDConnectionError
from app.services.lxd_service import (
    LXDService,
    get_lxd_service,
)
from pydantic import BaseModel, Field
from app.services.lxd.proxy import socket_timeout_for
from app.core.dependencies import require_tenant
from app.core.org_scope import org_scope_dependency, get_scope_cache_key, verify_cluster_access, check_tenant_permission
from app.core.single_flight import single_flight_on_fresh
from app.core.cache import get_cache_manager

logger = logging.getLogger(__name__)

# thread pool for parallel node queries
_ipam_executor = ThreadPoolExecutor(max_workers=10)

router = APIRouter(
    prefix="/ipam",
    tags=["IPAM"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)


class IPAMAllocation(BaseModel):
    """IPAM allocation information."""
    address: str = Field(..., description="IP address")
    type: str = Field(..., description="Address type (instance, network, etc)")
    used_by: str = Field(..., description="Resource using this address")
    hardware_address: str = Field("", description="MAC address")
    network: str = Field(..., description="Network name")
    node: str = Field(..., description="Node name")
    project: str = Field("default", description="Project name")


class IPAMListResponse(BaseModel):
    """Response for listing IPAM allocations."""
    total: int = Field(..., description="Total number of allocations")
    allocations: List[IPAMAllocation] = Field(..., description="List of IP allocations")
    nodes: List[str] = Field(..., description="List of nodes")
    unreachable_nodes: List[str] = Field(
        default_factory=list,
        description=(
            "Nodes that could not be reached. Their entries are missing from this list, "
            "so an unreachable node is distinguishable from one that has none."
        ),
    )


def _fetch_ipam_for_node(
    lxd_service: LXDService,
    node_name: str,
    network_filter: Optional[str],
) -> List[IPAMAllocation]:
    """Fetch IPAM allocations for a single node (runs in thread pool)."""
    allocations: List[IPAMAllocation] = []
    try:
        client = lxd_service.connection_manager.get_client(node_name)
        if client is None:
            # important: unreachable is not empty. Returning a bare collection
            # here leaves the caller nothing to label, which is how a dead
            # cluster read as a cluster with nothing.
            raise LXDConnectionError(f"Could not connect to {node_name}")

        # get project names
        try:
            projects = client.projects.all()
            project_names = [p.name for p in projects]
        except LXDConnectionError:
            # a missing client IS unreachability: let it through
            raise
        except Exception as e:
            # important: not unreachability. See networks.py: an item
            # vanishing mid-listing must not condemn the whole node.
            logger.warning(f"Could not get projects for {node_name}: {e}")
            project_names = ['default']

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

                if allocations_response.status_code == 200:
                    allocations_data = allocations_response.json().get('metadata', [])
                    for alloc in allocations_data:
                        alloc_network = alloc.get('network', '')
                        if network_filter and alloc_network != network_filter:
                            continue

                        addresses_str = alloc.get('addresses', '')
                        address = addresses_str.split('/')[0] if addresses_str else ''
                        alloc_type = alloc.get('type', 'network')
                        used_by = alloc.get('used_by', '')
                        hwaddr = alloc.get('hwaddr', '')

                        if used_by:
                            if '/instances/' in used_by:
                                instance_name = used_by.split('/instances/')[-1].split('?')[0]
                                resource_type = "instance"
                                resource_name = instance_name
                            else:
                                resource_type = alloc_type
                                resource_name = alloc_network
                        else:
                            resource_type = "network"
                            resource_name = alloc_network

                        if address:
                            allocations.append(IPAMAllocation(
                                address=address,
                                type=resource_type,
                                used_by=resource_name,
                                hardware_address=hwaddr or "",
                                network=alloc_network,
                                node=node_name,
                                project=project_name,
                            ))
                else:
                    logger.debug(
                        f"Could not get network allocations for project {project_name} on {node_name}: "
                        f"status {allocations_response.status_code}"
                    )
            except Exception as e:
                logger.debug(f"Error getting allocations for project {project_name} on {node_name}: {e}")
    except LXDConnectionError:
        # a missing client IS unreachability: let it through
        raise
    except Exception as e:
        # important: not unreachability. See networks.py: an item
        # vanishing mid-listing must not condemn the whole node.
        logger.warning(f"Error listing IPAM on {node_name}: {e}")
    return allocations


@router.get(
    "",
    response_model=IPAMListResponse,
    summary="List IPAM allocations",
    description="Get a list of all IP address allocations across networks.",
)
@single_flight_on_fresh("ipam:list")
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
    cache_key = get_scope_cache_key(f"ipam:list:host={host}:network={network}")
    cached = None if fresh else cache.get("api_responses", cache_key)
    if cached is not None:
        logger.debug(f"Returning cached IPAM data for {cache_key}")
        return IPAMListResponse(**cached)

    try:
        nodes_to_query = [host] if host else lxd_service.connection_manager.get_node_names()

        # query all nodes in parallel
        loop = asyncio.get_event_loop()
        tasks = [
            loop.run_in_executor(_ipam_executor, _fetch_ipam_for_node, lxd_service, node_name, network)
            for node_name in nodes_to_query
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_allocations: List[IPAMAllocation] = []
        # important: two different things make a node absent, and both must be named.
        # A node can fail during the fan-out, or never be asked at all, because
        # connection_manager.get_node_names() returns only nodes with a live client while
        # the fleet is whatever Vault has registered. Reporting only the first kind left a
        # dead cluster silently missing rather than merely empty.
        unreachable_nodes: List[str] = (
            [] if host else sorted(set(lxd_service.get_node_names()) - set(nodes_to_query))
        )
        answered = 0
        for node_name, result in zip(nodes_to_query, results):
            if isinstance(result, list):
                answered += 1
                all_allocations.extend(result)
            elif isinstance(result, Exception):
                logger.warning(f"IPAM node query failed: {result}")
                unreachable_nodes.append(node_name)

        all_allocations = [
            allocation for allocation in all_allocations
            if check_tenant_permission(
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
        )

        # important: an outage must not be remembered as the answer. See instances.py.
        if answered:
            cache.set("api_responses", cache_key, response.model_dump(), ttl=120)
        else:
            logger.warning("Not caching IPAM list: no node answered")
        return response
    except Exception as e:
        logger.error(f"Error listing IPAM: {e}")
        raise HTTPException(status_code=500, detail=str(e))
