"""
API Router for IPAM (IP Address Management) operations.
Provides endpoints for managing LXD network IP address allocations.
"""

from fastapi import APIRouter, HTTPException, Depends, Query
from typing import Any, Dict, Literal, Optional, List, Tuple
from urllib.parse import unquote
import logging
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

from app.services.lxd.exceptions import LXDConnectionError
from app.core.lxd_names import LXD_NAME_PATTERN
from app.services.lxd.fanout import LATE_FRESH_SECONDS, gather_clusters, NodeRead, collect, complete_reasons, unattempted
from app.services.lxd.ipam_join import classify, network_facts
from app.services.lxd.project_scope import (
    ALL_PROJECTS_READ_TIMEOUT,
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
    verify_cluster_access,
    check_tenant_permission,
    project_is_visible,
)
from app.core.tenant_visibility import tenant_acl_active
from app.core.cache import get_cache_manager
from app.core import swr
from app.core.swr import Built

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
# v3: the body moved into the stale-while-revalidate envelope and gained collected_at.
_CACHE_VERSION = "v3"
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
    node: str = Field(..., description="Cluster associated with this allocation")
    project: str = Field("default", description="Project name")
    allocation: AllocationKind = Field(
        "unknown",
        description=(
            "static when the instance's NIC on this network pins the address on the device "
            "(its own or inherited from a profile); dhcp when the instance holds it without a "
            "pin, which covers dnsmasq leases, OVN dynamic addresses and SLAAC; gateway, "
            "forward and load_balancer for the network's own rows; unknown when the instances "
            "of this cluster could not be read. A stale lease beside a fresh pin shows as one "
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
    nodes: List[str] = Field(..., description="Clusters included in this response")


def _read_allocations(
    client: Any, node_name: str, project_names: List[str], read: NodeRead
) -> List[Tuple[Optional[str], Dict[str, Any]]]:
    """Every allocation on the cluster as (listing project or None, row).

    important: one all-projects read where the daemon offers `network_allocations`, because the
    per-project walk lists a shared network's leases once per project that borrows the network:
    measured on a 19-project MicroCloud, 19 reads and 815 rows in 54 s against one read and
    59 rows in 4 s. The all-projects rows name their owner in used_by (`?project=`), which is
    what the caller reads; the listing project is None for them. A daemon without the
    extension, or an all-projects read that does not answer 200, is walked as before.
    """
    base_url = client.api._api_endpoint
    timeout = socket_timeout_for(client)
    info = getattr(client, "host_info", None) or {}
    if "network_allocations" in (info.get("api_extensions") or []):
        try:
            response = client.api.session.get(
                f"{base_url}/network-allocations?all-projects=true&recursion=1",
                timeout=ALL_PROJECTS_READ_TIMEOUT,
            )
            if response.status_code == 200:
                return [(None, row) for row in (response.json().get("metadata") or []) if isinstance(row, dict)]
            logger.debug(f"all-projects allocations on {node_name}: status {response.status_code}, walking projects")
        except Exception as e:  # noqa: BLE001 - fall back to the walk, which names its own losses
            logger.debug(f"all-projects allocations on {node_name} failed ({type(e).__name__}), walking projects")
    rows: List[Tuple[Optional[str], Dict[str, Any]]] = []
    for project_name in project_names:
        try:
            # important: client.api.session bypasses pylxd's _APINode, so the timeout
            # _APINode would have injected never applies. Without this the call blocks
            # forever against a silent node and permanently burns a pool thread.
            response = client.api.session.get(
                f"{base_url}/network-allocations?project={project_name}&recursion=1", timeout=timeout
            )
            if response.status_code != 200:
                logger.debug(
                    f"Could not get network allocations for project {project_name} on {node_name}: "
                    f"status {response.status_code}"
                )
                continue
            rows.extend((project_name, row) for row in (response.json().get("metadata") or []) if isinstance(row, dict))
        except Exception as e:  # noqa: BLE001 - one project's failure is named, not fatal
            # the project is in the log for the operator of the box; the banner a Partner on a
            # shared cluster reads must not name another organization's project
            logger.debug(f"Error getting allocations for project {project_name} on {node_name}: {e}")
            read.lost(f"one project's allocations: {type(e).__name__}")
    return rows


def _project_in_reference(tail: str) -> str:
    """The `?project=` of an instance reference, "default" when it carries none."""
    if "?" not in tail:
        return "default"
    for pair in tail.split("?", 1)[1].split("&"):
        if pair.startswith("project="):
            return unquote(pair[len("project="):]) or "default"
    return "default"


def _generation(cache: Any) -> Optional[str]:
    """The instance-list generation token, or None from a cache that keeps none (a stub)."""
    read = getattr(cache, "instance_list_generation", None)
    return read() if callable(read) else None


# The three reads _read_join_sources makes per cluster run side by side on their own pool, never
# on the outer per-cluster pool, so a full outer pool cannot wait on itself.
_JOIN_WORKERS = 12
_join_executor = ThreadPoolExecutor(max_workers=_JOIN_WORKERS, thread_name_prefix="ipam_join")


def _read_project_configs(client: Any, read: NodeRead) -> Dict[str, Dict[str, str]]:
    try:
        return list_project_configs(client)
    except Exception as e:  # noqa: BLE001 - named, not fatal
        read.lost(f"projects: {type(e).__name__}")
        return {"default": {}}


def _read_networks(client: Any, read: NodeRead) -> Dict[Tuple[str, str], Dict[str, Any]]:
    try:
        rows, complete = list_across_projects(client, "networks")
        if not complete:
            read.lost("networks: partial read")
        return {(str(r.get("project") or "default"), str(r.get("name") or "")): r for r in rows}
    except Exception as e:  # noqa: BLE001
        read.lost(f"networks: {type(e).__name__}")
        return {}


def _read_instances(client: Any, read: NodeRead) -> Tuple[Dict[Tuple[str, str], InstanceRecord], bool]:
    try:
        instance_rows, complete = list_instance_records(client)
        if not complete:
            read.lost("instances: partial read")
        return {(rec.project, rec.name): rec for rec in instance_rows}, complete
    except Exception as e:  # noqa: BLE001
        read.lost(f"instances: {type(e).__name__}")
        return {}, False


def _read_join_sources(
    client: Any, node_name: str, read: NodeRead
) -> Tuple[Dict[str, Dict[str, str]], Dict[Tuple[str, str], Dict[str, Any]], Dict[Tuple[str, str], InstanceRecord], bool]:
    """The projects, networks and instances of one cluster, each read once for the whole node.

    A failed read is named on the NodeRead and degrades the join rather than the listing: no
    networks means empty network facts, no instances means every instance row is "unknown".

    note: side by side since 2026-11-04. Read one after the other they summed on central-1
    (networks 5.3 s, instances 1.3 s at p50 on dev); now the join costs the slowest of the three.
    Each read keeps its own socket timeouts, so waiting on the futures cannot hang.
    """
    configs = _join_executor.submit(_read_project_configs, client, read)
    networks = _join_executor.submit(_read_networks, client, read)
    instances = _join_executor.submit(_read_instances, client, read)
    records, instances_ok = instances.result()
    return configs.result(), networks.result(), records, instances_ok


def _visible_allocation(allocation: IPAMAllocation) -> bool:
    """Whether the caller may see one allocation row.

    important: a Tenant keeps its own instances' rows only. A network, forward or load-balancer
    row names no instance, so `check_tenant_permission(..., None)` passed it on any rule in the
    project and handed a Tenant another party's forward listen addresses (often public IPs) and
    OVN subnets in a project they share (backlog A6, owner decision 2026-11-05: strict).
    """
    project = allocation.project or "default"
    if not project_is_visible(allocation.node, project):
        return False
    if allocation.type != "instance":
        return not tenant_acl_active()
    return check_tenant_permission(allocation.node, project, allocation.used_by, "view")


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
    # important: LXD lists a shared network's leases under every project that borrows the
    # network (features.networks unset), naming the instance's own project only in used_by.
    # The same lease therefore arrives once per borrowing project; it is one row, in the
    # instance's project, or the pin lookup misses and a tenant of the borrowing project is
    # shown an instance that is not theirs. The network's own rows (gateway, forwards) stay
    # per project: each borrower does use that gateway.
    seen_instance_rows: set = set()

    for listing_project, alloc in _read_allocations(client, node_name, project_names, read):
        alloc_network = alloc.get('network', '')
        if network_filter and alloc_network != network_filter:
            continue

        addresses_str = alloc.get('addresses', '')
        address = addresses_str.split('/')[0] if addresses_str else ''
        alloc_type = alloc.get('type', 'network')
        used_by = alloc.get('used_by', '')
        hwaddr = alloc.get('hwaddr', '')

        # the row's project: the instance's own for an instance row, the network's owner for the
        # network's rows when the daemon named it, else the project the row was listed under
        row_project = listing_project
        if used_by and '/instances/' in used_by:
            resource_type = "instance"
            tail = used_by.split('/instances/')[-1]
            resource_name = tail.split('?')[0]
            row_project = _project_in_reference(tail)
            key = (row_project, resource_name, alloc_network, address)
            if key in seen_instance_rows:
                continue
            seen_instance_rows.add(key)
        elif used_by:
            resource_type = alloc_type
            resource_name = alloc_network
            if listing_project is None:
                row_project = _project_in_reference(used_by.split('/networks/')[-1]) if '/networks/' in used_by else "default"
        else:
            resource_type = "network"
            resource_name = alloc_network
            if listing_project is None:
                row_project = "default"
        if not address:
            continue

        record = records.get((row_project, resource_name)) if resource_type == "instance" else None
        kind = classify(
            alloc_type if resource_type != "instance" else "instance",
            address, record, alloc_network, instances_ok,
        )
        net_type, net_project, subnet4, subnet6 = network_facts(
            alloc_network, row_project, configs, networks
        )
        read.rows.append(IPAMAllocation(
            address=address,
            type=resource_type,
            used_by=resource_name,
            hardware_address=hwaddr or "",
            network=alloc_network,
            node=node_name,
            project=row_project,
            allocation=kind,
            network_type=net_type,
            network_project=net_project,
            subnet_ipv4=subnet4,
            subnet_ipv6=subnet6,
        ))
    if read.problems:
        logger.warning("IPAM read on %s lost %d part(s): %s", node_name, len(read.problems), "; ".join(read.problems))
    return read


@router.get(
    "",
    response_model=IPAMListResponse,
    summary="List IPAM allocations",
    description="Get a list of all IP address allocations across networks.",
)
async def list_ipam_allocations(
    host: Optional[str] = Query(None, description="Filter by a specific cluster"),
    # The name pattern keeps an arbitrary string out of the cache and late-read memo keys, where
    # each distinct value would otherwise buy a fan-out that is never shared.
    network: Optional[str] = Query(None, pattern=LXD_NAME_PATTERN, description="Filter by specific network"),
    lxd_service: LXDService = Depends(get_lxd_service),
    fresh: bool = False,
):
    """List all IPAM allocations. Nodes are queried in parallel with caching."""
    if host:
        verify_cluster_access(host)
    # 120 s soft TTL (60 s when a cluster is down), then served for SWR_MAX_STALE_SECONDS while
    # one background read replaces it.
    cache = get_cache_manager()

    async def build() -> Built:
        nodes_to_query = [host] if host else lxd_service.connection_manager.get_node_names()

        # query all nodes in parallel, CLUSTER_WALL_SECONDS each (see fanout.gather_clusters)
        results = await gather_clusters(
            nodes_to_query,
            lambda node_name: _fetch_ipam_for_node(lxd_service, node_name, network),
            executor=_ipam_executor,
            label="ipam.list",
            memo_key=f"ipam:host={host}:network={network}",
            generation=lambda: _generation(cache),
        )

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
        cacheable = fan.cacheable_despite_outages and not unattempted(
            seeded, lxd_service.connection_manager
        )
        ttl = LATE_FRESH_SECONDS if fan.late_nodes else (DEGRADED_CACHE_TTL if unreachable_nodes else 120)

        all_allocations = [allocation for allocation in fan.rows if _visible_allocation(allocation)]

        response = IPAMListResponse(
            total=len(all_allocations),
            allocations=all_allocations,
            nodes=lxd_service.connection_manager.get_node_names(),
            unreachable_nodes=unreachable_nodes,
            partial_nodes=fan.partial_nodes,
            late_nodes=fan.late_nodes,
            node_errors=complete_reasons(
                unreachable_nodes, fan.node_errors, partial=fan.partial_nodes, late=fan.late_nodes
            ),
            collected_at=datetime.now(timezone.utc),
        )

        # important: an outage must not be remembered as the answer, and neither may a partial
        # one. A read that an invalidation overtook (a NIC pin, an instance lifecycle change) is
        # refused by swr.serve through the instance list generation. See instances.py and
        # storage_pools.py.
        if not cacheable:
            logger.warning("Not caching IPAM list: %s", fan.node_errors or "no node answered")
        return Built(value=response, body=response.model_dump(), fresh_for=ttl if cacheable else None)

    try:
        served = await swr.serve(
            cache=cache,
            base_key=f"ipam:list:{_CACHE_VERSION}:host={host}:network={network}",
            fresh=fresh,
            build=build,
            generation=lambda: _generation(cache),
            label="IPAM list",
        )
        return served.as_model(IPAMListResponse)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing IPAM: {e}")
        raise HTTPException(status_code=500, detail=str(e))
