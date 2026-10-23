"""
API Router for Network operations.
Provides endpoints for managing LXD networks.
"""

from fastapi import APIRouter, HTTPException, Depends, Query, Body
from typing import Optional, List, Dict
import logging
from datetime import datetime, timezone
import requests
import asyncio
from concurrent.futures import ThreadPoolExecutor

from app.services.lxd_service import (
    LXDService,
    LXDConnectionError,
    get_lxd_service,
)
from app.services.lxd.utils import jsonable_lxd, wait_for_lxd_operation, write_timeout
from pylxd.exceptions import LXDAPIException, ClientConnectionFailed
from app.core.dependencies import check_read_only_mode, require_tenant, require_partner, AuthenticatedUser
from app.core.org_scope import (
    org_scope_dependency,
    verify_cluster_access,
    project_is_visible,
)
from app.services.lxd.cluster_topology import cluster_member_names, is_clustered
from app.services.lxd.project_scope import (
    DEFAULT_PROJECT,
    effective_project,
    list_across_projects,
    list_in_project,
    list_project_configs,
    project_params,
    resolve_owner,
)
from app.core.tenant_visibility import (
    filter_visible_lxd_references,
    tenant_acl_active,
    tenant_may_see_project,
)
from app.core.cache import get_cache_manager, get_resource_list_generation
from app.core.blocking import (
    BlockingPoolFull,
    BlockingTimeout,
    in_flight_for_key,
    run_blocking,
)
from pydantic import BaseModel, Field
from app.services.network_config import (
    ALLOWED_NETWORK_CONFIG_KEYS,  # noqa: F401 - compatibility export for callers and tests
    NetworkCreateRequest,
    validate_network_config,
)
from app.services.network_capabilities import (
    NetworkCapabilities,
    network_capabilities,
    validate_network_capabilities,
)
from app.services.network_write_verification import (
    require_network_readback,
    require_network_update_readback,
)
from app.services.activity_logger import get_activity_logger
from app.core.lxd_path import safe_segment
from app.core.database import get_db
from app.core import swr
from app.core.swr import Built
from app.services.shared_cluster_audit import serialize_cluster_configuration
from app.services.migration_fence import (
    MigrationFenceConflict,
    assert_cluster_not_migration_fenced,
    assert_project_not_migration_fenced,
)
from sqlalchemy.ext.asyncio import AsyncSession
from types import SimpleNamespace

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

router = APIRouter(
    prefix="/networks",
    tags=["Networks"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)

# Thread pool for parallel operations
_executor = ThreadPoolExecutor(max_workers=5)

# Wall-clock cap for a create. On a cluster the call is N+4 sequential round trips, each
# already bounded to the pooled (3, 8) by TimeoutHTTPAdapter, so this only has to cover a
# realistic member count rather than an unbounded wait.
NETWORK_CREATE_TIMEOUT = 90.0
# important: LXD 6.9 (api extension storage_and_network_operations) answers a network create,
# update or delete with an operation instead of finishing first. Read back before it settles
# and the daemon says "Network not found" about a network it is still creating; list after a
# delete and the row is still there. Every write below waits on the operation it got, which
# on an older daemon is a no-op because there is none.
NETWORK_WRITE_WAIT_SECONDS = 60
# Shed rather than queue: the pool is 5 wide and shared with the list fan-out.
NETWORK_MAX_IN_FLIGHT = 10
NETWORK_MAX_IN_FLIGHT_PER_CLUSTER = 1


def _invalidate_network_lists() -> None:
    """A network write changes the networks list and the gateway rows IPAM shows for it.

    IPAM caches for 120 s and used to be left alone, so a network created or deleted here kept
    its old gateway row on the IPAM page until the TTL ran out.
    """
    cache = get_cache_manager()
    # one family per call: some cache stand-ins accept a single kind
    cache.invalidate_resource_cache("networks")
    cache.invalidate_resource_cache("ipam")


async def _run_network_blocking(
    function,
    *args,
    host: str,
    label: str,
):
    """Keep the cluster lock until an admitted worker has actually stopped."""
    try:
        return await run_blocking(
            function,
            *args,
            timeout=NETWORK_CREATE_TIMEOUT,
            label=label,
            executor=_executor,
            max_in_flight=NETWORK_MAX_IN_FLIGHT,
            concurrency_key=host,
            max_in_flight_per_key=NETWORK_MAX_IN_FLIGHT_PER_CLUSTER,
        )
    except (BlockingTimeout, asyncio.CancelledError):
        # A timeout or task cancellation cannot stop a worker thread. The daemon calls have
        # bounded socket timeouts, so drain the one per-cluster worker before the caller may
        # release migration admission and allow a fence to be created.
        while in_flight_for_key(_executor, host):
            try:
                await asyncio.shield(asyncio.sleep(0.05))
            except asyncio.CancelledError:
                continue
        raise


class NetworkInfo(BaseModel):
    """Network information."""
    name: str = Field(..., description="Network name")
    description: str = Field("", description="Network description")
    type: str = Field(..., description="Network type (bridge, physical, etc)")
    managed: bool = Field(True, description="Whether network is managed by LXD")
    config: Dict[str, str] = Field(default_factory=dict, description="Network configuration")
    used_by: List[str] = Field(default_factory=list, description="Instances using this network")
    status: str = Field("", description="Network status")
    node: str = Field("", description="Cluster where the network is defined")
    project: str = Field(
        DEFAULT_PROJECT,
        description="LXD project that holds this network",
    )


# An OVN network reaches the outside world through an uplink, and LXD accepts only a managed
# Bridge or Physical network for the job. The dashboard used to hand the daemon whatever the
# operator picked, and the picker offered every interface it could see: other OVN networks,
# macvlan interfaces, and the loopback. On one LXD 6.9 host that is 11 candidates LXD cannot
# accept out of 23 offered. Choosing one produced a failure at the daemon, or an instance that
# never finished creating.
OVN_UPLINK_TYPES = frozenset({"bridge", "physical"})

# Network types that carry member-specific config on a cluster, and so must be defined on
# every member (POST ?target=) before the cluster-wide create. OVN is the one type without
# any, and LXD refuses the define step for it. See create_network.
MEMBER_DEFINED_NETWORK_TYPES = frozenset({"bridge", "macvlan", "physical", "sriov"})


def _uplink_candidates(client, host: str) -> Dict[str, Dict]:
    """Networks on `host` that could serve as an OVN uplink, by name.

    Read from the default project: LXD resolves an OVN network's uplink there, not in the
    project the OVN network itself lives in.
    """
    candidates: Dict[str, Dict] = {}
    for row in list_in_project(client, "networks", DEFAULT_PROJECT):
        name = row.get("name")
        if name:
            candidates[name] = row
    return candidates


def _restricted_uplinks(project_config: Dict[str, str]) -> Optional[set]:
    """The uplinks a project is allowed to use, or None when it is not restricted.

    important: LXD ignores the whole `restricted.*` family unless `restricted` is true, so
    honouring the list unconditionally would wrongly narrow the choice for every ordinary
    project. Most projects on this fleet set `restricted: false` explicitly.
    """
    if str(project_config.get("restricted", "false")).lower() != "true":
        return None
    raw = project_config.get("restricted.networks.uplinks")
    if raw is None:
        return None
    return {part.strip() for part in raw.split(",") if part.strip()}


def validate_uplink(
    request: "NetworkCreateRequest",
    candidates: Dict[str, Dict],
    project_config: Optional[Dict[str, str]] = None,
) -> None:
    """Refuse an OVN uplink LXD would refuse, and say which ones it would take.

    Raises HTTPException(400). Fail fast here rather than let the daemon reject it later:
    a create that reaches LXD and fails leaves the operator with a spinner and no reason.
    """
    uplink = (request.config or {}).get("network")

    if request.type != "ovn":
        if uplink:
            raise HTTPException(
                status_code=400,
                detail=(
                    "The 'network' key names an OVN uplink and only applies to an OVN "
                    f"network, not a {request.type} one."
                ),
            )
        return

    usable = sorted(
        name
        for name, row in candidates.items()
        if row.get("type") in OVN_UPLINK_TYPES and row.get("managed")
    )
    allowed = _restricted_uplinks(project_config or {})
    if allowed is not None:
        usable = [name for name in usable if name in allowed]

    def _offer() -> str:
        return (
            f" Usable uplinks in this cluster: {', '.join(usable)}."
            if usable
            else " No network in this cluster can serve as an uplink; create a managed bridge or"
            " physical network first."
        )

    if not uplink:
        raise HTTPException(
            status_code=400,
            detail="An OVN network needs an uplink network." + _offer(),
        )

    row = candidates.get(uplink)
    if row is None:
        raise HTTPException(
            status_code=400,
            detail=f"Uplink network '{uplink}' does not exist in this cluster." + _offer(),
        )
    if row.get("type") not in OVN_UPLINK_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Uplink network '{uplink}' is of type {row.get('type')}. An OVN network can "
                "only use a managed bridge or physical network as its uplink." + _offer()
            ),
        )
    if not row.get("managed"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Uplink network '{uplink}' is not managed by LXD, so it cannot carry an OVN "
                "uplink." + _offer()
            ),
        )
    if allowed is not None and uplink not in allowed:
        raise HTTPException(
            status_code=400,
            detail=(
                f"This project restricts uplinks and '{uplink}' is not among them." + _offer()
            ),
        )



# Maps a daemon error onto a status and strips URLs and filesystem paths out of the text
# before it reaches the client.
#
# note: both used to be defined here, and this file was the only correct copy in the backend
# while 169 other handlers passed `str(e)` through untouched. They now live in app/core so the
# global 5xx handler and the image and storage-volume delete paths share one definition
# rather than growing variants that drift.
from app.core.lxd_errors import lxd_http_error, lxd_transport_error  # noqa: E402


# note: module-level aliases, not direct imports at the call sites. create_network resolves
# these by name at call time, which is what lets a test swap the cluster shape.
_is_clustered = is_clustered
_cluster_member_names = cluster_member_names


def _lxd_http_error(exc: Exception, network_name: str) -> HTTPException:
    """Map an LXD/daemon error onto a meaningful status instead of a blanket 500."""
    # note: the mapping moved to app/core/lxd_errors so the image and storage-volume delete
    # paths share it. This wrapper stays because five call sites below read better naming
    # the network once rather than repeating the noun at each raise.
    return lxd_http_error(exc, "Network", network_name)


class NetworkListResponse(BaseModel):
    """Response for listing networks."""
    total: int = Field(..., description="Total number of networks")
    networks: List[NetworkInfo] = Field(..., description="List of networks")
    nodes: List[str] = Field(..., description="Clusters included in this response")
    unreachable_nodes: List[str] = Field(
        default_factory=list,
        description=(
            "Clusters that could not be reached. Their networks are missing from this "
            "list, so an unreachable cluster is distinguishable from one with none."
        ),
    )
    partial_nodes: List[str] = Field(
        default_factory=list,
        description=(
            "Clusters that answered but could not be read for every project, so their networks "
            "may be incomplete. A short list is never presented as the whole truth."
        ),
    )
    collected_at: Optional[datetime] = Field(
        None, description="When this list was read. A cached answer keeps the time of its read."
    )


def _fetch_networks_for_node(
    lxd_service: LXDService, node_name: str, project: Optional[str] = None
) -> tuple[str, Optional[List[NetworkInfo]], bool]:
    """Fetch networks for a single node - runs in thread pool.

    Returns (node_name, networks, complete). `networks` is None when the node is
    unreachable, so the caller can tell "node is down" apart from "node has no
    networks" (previously both looked like an empty list).

    With no project asked for this reads every project on the node. Naming one reads the
    project that actually holds its networks, which is the project itself only when it sets
    features.networks; otherwise it sees the default project's.
    """
    networks_list: List[NetworkInfo] = []
    complete = True
    try:
        client = lxd_service.get_client_with_reconnect(node_name)
        if client is None:
            logger.warning(f"Could not connect to {node_name}, skipping for networks list")
            return node_name, None, True
        if project:
            owner = resolve_owner(client, "networks", project)
            rows = list_in_project(client, "networks", owner)
        else:
            rows, complete = list_across_projects(client, "networks")
        for row in rows:
            networks_list.append(NetworkInfo(
                name=row.get("name") or "",
                description=row.get("description") or "",
                type=row.get("type") or "",
                managed=bool(row.get("managed")),
                config=row.get("config") or {},
                used_by=row.get("used_by") or [],
                status=row.get("status") or "",
                node=node_name,
                project=row.get("project") or DEFAULT_PROJECT,
            ))
    except Exception as e:
        # important: an error while listing is not the same as an unreachable node, and
        # conflating them is user-visible. A network disappearing mid-iteration raises
        # "Network not found", and treating that as unreachability wiped every network on a
        # healthy node and told the operator the node was down. Reproduced by listing while
        # a network was being deleted. Report what was collected instead.
        logger.warning(f"Partial network listing on {node_name}: {e}")
        return node_name, networks_list, False
    return node_name, networks_list, complete


@router.get(
    "",
    response_model=NetworkListResponse,
    summary="List all networks",
    description="Get a list of all networks from connected clusters.",
)
async def list_networks(
    host: Optional[str] = Query(None, description="Filter by a specific cluster"),
    project: Optional[str] = Query(
        None,
        description=(
            "LXD project to read. Omit for every project, each row naming the one that "
            "holds it."
        ),
    ),
    lxd_service: LXDService = Depends(get_lxd_service),
    fresh: bool = False,
):
    """List all networks."""
    if host:
        verify_cluster_access(host)
    cache = get_cache_manager()

    async def build() -> Built:
        nodes_to_query = [host] if host else lxd_service.connection_manager.get_node_names()
        
        # Fetch networks from all nodes in parallel
        loop = asyncio.get_event_loop()
        tasks = [
            loop.run_in_executor(
                _executor, _fetch_networks_for_node, lxd_service, node_name, project
            )
            for node_name in nodes_to_query
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Flatten results, tracking nodes we could not reach so an unreachable
        # cluster isn't silently reported as "0 networks".
        all_networks = []
        # important: two different things make a node absent, and both must be named.
        # A node can fail during the fan-out, or never be asked at all, because
        # connection_manager.get_node_names() returns only nodes with a live client while
        # the fleet is whatever Vault has registered. Reporting only the first kind left a
        # dead cluster silently missing rather than merely empty.
        unreachable_nodes: List[str] = (
            [] if host else sorted(set(lxd_service.get_node_names()) - set(nodes_to_query))
        )
        answered = 0
        partial_nodes: List[str] = []
        for result in results:
            if isinstance(result, tuple):
                node_name, networks, complete = result
                if networks is None:
                    unreachable_nodes.append(node_name)
                else:
                    answered += 1
                    all_networks.extend(networks)
                    if not complete:
                        partial_nodes.append(node_name)
            elif isinstance(result, Exception):
                logger.warning(f"Error fetching networks: {result}")

        # important: a Partner is scoped by cluster, not by project, and the used_by filter
        # below never runs for them. On a cluster shared with another organization this is the
        # only thing standing between them and that organization's network configuration.
        all_networks = [n for n in all_networks if project_is_visible(n.node, n.project)]
        # used_by is held to the caller's bound for every role; only a tenant loses the row
        # when nothing they may see uses it.
        for network in all_networks:
            network.used_by = filter_visible_lxd_references(network.node, network.used_by)

        if tenant_acl_active():
            visible_networks = []
            for network in all_networks:
                # ownership as well as usage: see tenant_may_see_project
                if not tenant_may_see_project(network.node, network.project):
                    continue
                if network.used_by:
                    visible_networks.append(network)
            all_networks = visible_networks

        # Store in variable first
        response = NetworkListResponse(
            total=len(all_networks),
            networks=all_networks,
            nodes=lxd_service.connection_manager.get_node_names(),
            unreachable_nodes=sorted(unreachable_nodes),
            partial_nodes=sorted(partial_nodes),
            collected_at=datetime.now(timezone.utc),
        )
        
        # important: an outage must not be remembered as the answer. See instances.py.
        if not answered:
            logger.warning("Not caching networks list: no node answered")
        return Built(value=response, body=response.model_dump(), fresh_for=180 if answered else None)

    try:
        # important: the project belongs in the key. get_scope_cache_key mixes in the cluster
        # scope and the ACL digest but not the query, so without it a project-scoped read and a
        # fleet-wide one collide on one entry for the whole TTL.
        # e1: the body moved into the stale-while-revalidate envelope and gained collected_at.
        served = await swr.serve(
            cache=cache,
            base_key=f"networks:list:e1:host={host}:project={project or '*'}",
            fresh=fresh,
            build=build,
            generation=lambda: get_resource_list_generation(cache, "networks"),
            label="networks list",
        )
        return served.as_model(NetworkListResponse)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing networks: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/creation/capabilities", response_model=NetworkCapabilities)
async def get_network_creation_capabilities(
    host: str = Query(..., description="Target cluster"),
    lxd_service: LXDService = Depends(get_lxd_service),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Read supported create options without exposing server configuration."""
    verify_cluster_access(host)

    def _read():
        client = lxd_service.get_client_with_reconnect(host)
        if client is None:
            raise HTTPException(status_code=503, detail="Could not read hypervisor network capabilities")
        return network_capabilities(client)

    try:
        return await _run_network_blocking(_read, host=host, label="network_creation_capabilities")
    except BlockingTimeout:
        raise HTTPException(status_code=504, detail="Network capability read timed out") from None
    except BlockingPoolFull:
        raise HTTPException(status_code=503, detail="Network operations saturated; please retry shortly") from None
    except (LXDAPIException, ClientConnectionFailed, LXDConnectionError, requests.RequestException):
        raise HTTPException(status_code=503, detail="Could not read hypervisor network capabilities") from None


def _validate_network_acls(client, config: Dict[str, str], owner: str) -> None:
    selected = {name for name in config.get("security.acls", "").split(",") if name}
    if not selected:
        return
    available = {row.get("name") for row in list_in_project(client, "network-acls", owner)}
    missing = sorted(selected - available)
    if missing:
        raise HTTPException(status_code=400, detail="Network ACLs are not available in this project: "
                            + ", ".join(missing))


@router.get(
    "/{network_name}",
    response_model=NetworkInfo,
    summary="Get network details",
    description="Get detailed information about a specific network.",
)
async def get_network(
    network_name: str,
    host: str = Query(..., description="Target cluster"),
    project: Optional[str] = Query(
        None, description="LXD project holding the network (default project when omitted)"
    ),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get details for a specific network."""
    verify_cluster_access(host)
    try:
        client = lxd_service.get_client_with_reconnect(host)
        if client is None:
            raise HTTPException(
                status_code=503,
                detail=f"Could not connect to cluster '{host}'",
            )
        owner = resolve_owner(client, "networks", project)
        if not project_is_visible(host, owner):
            raise HTTPException(status_code=403, detail="Access denied to this network")
        data = client.api.networks[safe_segment(network_name)].get(
            params=project_params(owner)
        ).json().get("metadata") or {}
        used_by = filter_visible_lxd_references(host, data.get("used_by") or [])
        if tenant_acl_active() and not used_by:
            raise HTTPException(status_code=403, detail="Access denied to this network")

        return NetworkInfo(
            name=data.get("name") or network_name,
            description=data.get("description") or "",
            type=data.get("type") or "",
            managed=bool(data.get("managed")),
            config=data.get("config") or {},
            used_by=used_by,
            status=data.get("status") or "",
            node=host,
            project=data.get("project") or owner,
        )
    except HTTPException:
        # 503 (unreachable) / 403 (tenant denied) must survive. Previously the
        # generic handler collapsed everything into a 404 "not found", so a real
        # but unreachable network read as deleted.
        raise
    except (LXDAPIException, ClientConnectionFailed) as e:
        logger.error(f"LXD error getting network {network_name} on {host}: {e}")
        raise _lxd_http_error(e, network_name)
    except LXDConnectionError as e:
        logger.warning(f"LXD connection error getting network {network_name}: {e}")
        raise HTTPException(status_code=503, detail=f"Could not reach cluster '{host}'")
    except Exception as e:
        logger.error(f"Unexpected error getting network {network_name}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get network")


@router.post(
    "",
    response_model=dict,
    summary="Create network",
    description="Create a new network.",
)
async def create_network(
    request: NetworkCreateRequest,
    host: str = Query(..., description="Target cluster"),
    project: Optional[str] = Query(
        None, description="LXD project to create the network in"
    ),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Create a new network."""
    verify_cluster_access(host)
    requested_project = project or DEFAULT_PROJECT
    if not project_is_visible(host, requested_project):
        raise HTTPException(status_code=403, detail="Access denied to this project")

    def _resolve_target_blocking():
        client = lxd_service.get_client_with_reconnect(host)
        if client is None:
            raise HTTPException(
                status_code=503,
                detail=f"Could not connect to cluster '{host}'",
            )
        project_configs = list_project_configs(client) if project else {}
        owner = effective_project("networks", project, project_configs)
        return client, project_configs, owner

    def _create_blocking(client, project_configs, owner):
        """Run validation, daemon submission, operation wait, and readback off the loop.

        important: on a cluster this is N+3 sequential HTTP round trips after target
        resolution (cluster probe, member list, one define per member, the cluster-wide
        create, the read-back), so it scales with cluster size.
        """
        # important: resolved, not taken as given. A project that does not set
        # features.networks holds no networks of its own and LXD stores this in default;
        # writing it as asked and reading it back as resolved would disagree about where it
        # went.
        # important: LXD refuses any network type except OVN in a project other than default,
        # with "Network type does not support non-default projects". Verified against LXD 6.9.
        # Saying so here is the difference between an operator learning the rule and an
        # operator reading a daemon string, and it is why a project-scoped network on a
        # cluster is an OVN one.
        if owner != DEFAULT_PROJECT and request.type != "ovn":
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Only OVN networks can be created in project '{owner}'. A "
                    f"{request.type} network always lives in the default project, which is "
                    "also where an OVN network looks for its uplink."
                ),
            )

        validate_network_capabilities(client, request.type, request.config)
        _validate_network_acls(client, request.config, owner)
        validate_uplink(
            request,
            _uplink_candidates(client, host),
            project_configs.get(project or DEFAULT_PROJECT, {}),
        )
        scope = project_params(owner)

        if _is_clustered(client) and request.type in MEMBER_DEFINED_NETWORK_TYPES:
            # Clustered LXD: a network must first be DEFINED on every member
            # (POST ?target=<member>, member-scoped, no cluster-wide config) and
            # then CREATED cluster-wide (POST with no target). pylxd 2.4.1's
            # networks.create() has no target parameter, so drive the raw API.
            #
            # important: only for the types that carry member-specific config. An OVN
            # network is a cluster-wide object with none, and the daemon refuses the define
            # step outright: 'Network type "ovn" does not support member specific config'.
            # Measured against a 3-member MicroCloud: every OVN create from the console
            # answered 400 while a bridge went through, so the one network type a MicroCloud
            # tenant can create in its project was the one it could not create.
            for member in _cluster_member_names(client):
                wait_for_lxd_operation(
                    client,
                    client.api.networks.post(
                        json={"name": request.name, "type": request.type},
                        params={**scope, "target": member},
                        timeout=write_timeout(NETWORK_WRITE_WAIT_SECONDS),
                    ),
                    timeout=NETWORK_WRITE_WAIT_SECONDS,
                    project=owner,
                )
            wait_for_lxd_operation(
                client,
                client.api.networks.post(
                    json={
                        "name": request.name,
                        "type": request.type,
                        "config": request.config,
                        "description": request.description,
                    },
                    params=scope,
                    timeout=write_timeout(NETWORK_WRITE_WAIT_SECONDS),
                ),
                timeout=NETWORK_WRITE_WAIT_SECONDS,
                project=owner,
            )
        else:
            wait_for_lxd_operation(
                client,
                client.api.networks.post(
                    json={
                        "name": request.name,
                        "type": request.type,
                        "config": request.config,
                        "description": request.description,
                    },
                    params=scope,
                    timeout=write_timeout(NETWORK_WRITE_WAIT_SECONDS),
                ),
                timeout=NETWORK_WRITE_WAIT_SECONDS,
                project=owner,
            )
        created = client.api.networks[safe_segment(request.name)].get(params=scope).json().get("metadata")
        return require_network_readback(created, request.name, request.type, owner)

    submission_started = False
    try:
        try:
            async with serialize_cluster_configuration(db, host):
                try:
                    await assert_cluster_not_migration_fenced(cluster_id=host, db=db)
                except MigrationFenceConflict as exc:
                    raise HTTPException(status_code=409, detail=exc.detail) from exc
                client, project_configs, owner = await _run_network_blocking(
                    _resolve_target_blocking,
                    host=host,
                    label=f"resolve_network_owner[{host}/{request.name}]",
                )
                if not project_is_visible(host, owner):
                    raise HTTPException(status_code=403, detail="Access denied to this project")
                for fenced_project in sorted({requested_project, owner}):
                    try:
                        await assert_project_not_migration_fenced(
                            cluster_id=host,
                            project=fenced_project,
                            db=db,
                        )
                    except MigrationFenceConflict as exc:
                        raise HTTPException(status_code=409, detail=exc.detail) from exc
                submission_started = True
                network = await _run_network_blocking(
                    _create_blocking,
                    client,
                    project_configs,
                    owner,
                    host=host,
                    label=f"create_network[{host}/{request.name}]",
                )
        except BlockingTimeout as timeout_error:
            logger.error(f"create_network timed out on {host} for {request.name}")
            if submission_started:
                # the create goes on in its worker thread; clear the lists when it actually ends
                timeout_error.when_finished(_invalidate_network_lists)
            if not submission_started:
                raise HTTPException(
                    status_code=504,
                    detail=(
                        f"Network target resolution on {host} exceeded "
                        f"{NETWORK_CREATE_TIMEOUT:.0f}s; no network write was submitted"
                    ),
                ) from None
            # The define/create fan-out may have partially applied; say so rather than
            # implying nothing happened.
            raise HTTPException(
                status_code=504,
                detail=(
                    f"Network creation on {host} exceeded {NETWORK_CREATE_TIMEOUT:.0f}s and may "
                    "be partially applied; re-check the network list before retrying"
                ),
            ) from None
        except BlockingPoolFull:
            raise HTTPException(
                status_code=503, detail="Network operations saturated; please retry shortly"
            ) from None

        # Log activity: network creation success
        activity_logger.log_activity(
            action="network.create",
            target_type="network",
            target_id=request.name,
            target_host=host,
            result="success",
            details={"type": request.type, "description": request.description}
        )

        # Invalidate the (180s) networks list cache so the new network shows
        # immediately instead of after the TTL. Scope-aware keys look like
        # "networks:list:host=...:scope=...", so clear by prefix.
        _invalidate_network_lists()

        return {
            "success": True,
            "message": f"Network {request.name} created successfully",
            "network": NetworkInfo(
                name=network.get("name") or request.name,
                description=network.get("description") or "",
                type=network.get("type") or request.type,
                managed=bool(network.get("managed", True)),
                config=network.get("config") or {},
                used_by=network.get("used_by") or [],
                status=network.get("status") or "",
                node=host,
                project=network.get("project") or DEFAULT_PROJECT,
            )
        }
    except HTTPException:
        raise  # 503 (unreachable) must not be re-raised as a 500
    except (LXDAPIException, ClientConnectionFailed, LXDConnectionError) as e:
        logger.error(f"LXD error creating network {request.name} on {host}: {e}")
        activity_logger.log_activity(
            action="network.create",
            target_type="network",
            target_id=request.name,
            target_host=host,
            result="failure",
            error_message=str(e),
        )
        raise _lxd_http_error(e, request.name)
    except Exception as e:
        logger.error(f"Unexpected error creating network {request.name}: {e}", exc_info=True)
        activity_logger.log_activity(
            action="network.create",
            target_type="network",
            target_id=request.name,
            target_host=host,
            result="failure",
            error_message=str(e),
        )
        # Generic message: raw daemon strings can leak internal paths/hostnames.
        raise HTTPException(status_code=500, detail="Failed to create network")


@router.put(
    "/{network_name}",
    response_model=dict,
    summary="Update network",
    description="Update an existing network.",
)
async def update_network(
    network_name: str,
    host: str = Query(..., description="Target cluster"),
    project: Optional[str] = Query(
        None, description="LXD project holding the network (default project when omitted)"
    ),
    config: Optional[Dict[str, str]] = Body(None),
    description: Optional[str] = Body(None, max_length=1024),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Update a network."""
    verify_cluster_access(host)
    # Same allow-list as create: config is passed straight to the daemon.
    if config is not None:
        try:
            config = validate_network_config(config, allow_unset=True)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    requested_project = project or DEFAULT_PROJECT
    if not project_is_visible(host, requested_project):
        raise HTTPException(status_code=403, detail="Access denied to this network")

    def _resolve_update_target():
        client = lxd_service.get_client_with_reconnect(host)
        if client is None:
            raise HTTPException(status_code=503, detail=f"Could not connect to cluster '{host}'")
        return client, resolve_owner(client, "networks", project)

    def _update_blocking(client, owner):
        scope = project_params(owner)
        api = client.api.networks[safe_segment(network_name)]
        current = api.get(params=scope).json().get("metadata")
        if not isinstance(current, dict) or not current.get("type"):
            raise HTTPException(status_code=502, detail="Hypervisor did not return a valid network before update")
        validated_config = None
        if config is not None:
            try:
                validated_config = validate_network_config(config, current["type"], allow_unset=True)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
            if current["type"] in {"bridge", "ovn"}:
                validate_network_capabilities(client, current["type"], validated_config)
            _validate_network_acls(client, validated_config, owner)

        if validated_config is not None and "network" in validated_config:
            merged = {**(current.get("config") or {}), **validated_config}
            probe = SimpleNamespace(type=current["type"], config=merged)
            validate_uplink(
                probe,
                _uplink_candidates(client, host),
                list_project_configs(client).get(owner, {}),
            )
        payload = {
            "config": current.get("config") or {},
            "description": current.get("description") or "",
        }
        if validated_config is not None:
            payload["config"] = validated_config
        if description is not None:
            payload["description"] = description
        wait_for_lxd_operation(
            client,
            api.put(json=payload, params=scope, timeout=write_timeout(NETWORK_WRITE_WAIT_SECONDS)),
            timeout=NETWORK_WRITE_WAIT_SECONDS,
            project=owner,
        )
        observed = api.get(params=scope).json().get("metadata")
        require_network_update_readback(
            observed, network_name, current["type"], owner, validated_config, description,
        )
        _invalidate_network_lists()

    submission_started = False
    try:
        async with serialize_cluster_configuration(db, host):
            try:
                await assert_cluster_not_migration_fenced(cluster_id=host, db=db)
                client, owner = await _run_network_blocking(
                    _resolve_update_target, host=host, label=f"resolve_network_update_owner[{host}/{network_name}]",
                )
                if not project_is_visible(host, owner):
                    raise HTTPException(status_code=403, detail="Access denied to this network")
                for fenced_project in sorted({requested_project, owner}):
                    await assert_project_not_migration_fenced(
                        cluster_id=host,
                        project=fenced_project,
                        db=db,
                    )
            except MigrationFenceConflict as exc:
                raise HTTPException(status_code=409, detail=exc.detail) from exc
            submission_started = True
            await _run_network_blocking(
                _update_blocking, client, owner, host=host, label=f"update_network[{host}/{network_name}]",
            )

        return {
            "success": True,
            "message": f"Network {network_name} updated successfully"
        }
    except BlockingTimeout as timeout_error:
        if submission_started:
            timeout_error.when_finished(_invalidate_network_lists)
        detail = (
            "Network update timed out and may be partially applied; re-check its state before retrying"
            if submission_started else "Network target resolution timed out; no update was submitted"
        )
        raise HTTPException(status_code=504, detail=detail) from None
    except BlockingPoolFull:
        raise HTTPException(status_code=503, detail="Network operations saturated; please retry shortly") from None
    except HTTPException:
        raise
    except (LXDAPIException, ClientConnectionFailed, LXDConnectionError) as e:
        logger.error(f"LXD error updating network {network_name} on {host}: {e}")
        raise _lxd_http_error(e, network_name)
    except Exception as e:
        logger.error(f"Unexpected error updating network {network_name}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to update network")


@router.delete(
    "/{network_name}",
    response_model=dict,
    summary="Delete network",
    description="Delete a network.",
)
async def delete_network(
    network_name: str,
    host: str = Query(..., description="Target cluster"),
    project: Optional[str] = Query(
        None, description="LXD project holding the network (default project when omitted)"
    ),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Delete a network."""
    verify_cluster_access(host)
    requested_project = project or DEFAULT_PROJECT
    if not project_is_visible(host, requested_project):
        raise HTTPException(status_code=403, detail="Access denied to this network")
    try:
        async with serialize_cluster_configuration(db, host):
            client = lxd_service.get_client_with_reconnect(host)
            if client is None:
                raise HTTPException(
                    status_code=503,
                    detail=f"Could not connect to cluster '{host}'",
                )
            owner = resolve_owner(client, "networks", project)
            if not project_is_visible(host, owner):
                raise HTTPException(status_code=403, detail="Access denied to this network")
            try:
                await assert_cluster_not_migration_fenced(cluster_id=host, db=db)
                for fenced_project in sorted({requested_project, owner}):
                    await assert_project_not_migration_fenced(
                        cluster_id=host,
                        project=fenced_project,
                        db=db,
                    )
            except MigrationFenceConflict as exc:
                raise HTTPException(status_code=409, detail=exc.detail) from exc
            wait_for_lxd_operation(
                client,
                client.api.networks[safe_segment(network_name)].delete(
                    params=project_params(owner), timeout=write_timeout(NETWORK_WRITE_WAIT_SECONDS)
                ),
                timeout=NETWORK_WRITE_WAIT_SECONDS,
            )

        _invalidate_network_lists()

        # Log activity: network deletion success
        activity_logger.log_activity(
            action="network.delete",
            target_type="network",
            target_id=network_name,
            target_host=host,
            result="success"
        )
        
        return {
            "success": True,
            "message": f"Network {network_name} deleted successfully"
        }
    except HTTPException:
        raise
    except (LXDAPIException, ClientConnectionFailed, LXDConnectionError) as e:
        logger.error(f"LXD error deleting network {network_name} on {host}: {e}")
        activity_logger.log_activity(
            action="network.delete",
            target_type="network",
            target_id=network_name,
            target_host=host,
            result="failure",
            error_message=str(e),
        )
        # e.g. an unmanaged network, or one still in use -> 409/400, not 500.
        raise _lxd_http_error(e, network_name)
    except Exception as e:
        logger.error(f"Unexpected error deleting network {network_name}: {e}", exc_info=True)
        activity_logger.log_activity(
            action="network.delete",
            target_type="network",
            target_id=network_name,
            target_host=host,
            result="failure",
            error_message=str(e),
        )
        raise HTTPException(status_code=500, detail="Failed to delete network")


@router.get(
    "/{network_name}/state",
    response_model=dict,
    summary="Get network state",
    description="Get the current state of a network.",
)
async def get_network_state(
    network_name: str,
    host: str = Query(..., description="Target cluster"),
    project: Optional[str] = Query(
        None, description="LXD project holding the network (default project when omitted)"
    ),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get network state."""
    verify_cluster_access(host)
    try:
        client = lxd_service.get_client_with_reconnect(host)
        if client is None:
            raise HTTPException(
                status_code=503,
                detail=f"Could not connect to cluster '{host}'",
            )
        owner = resolve_owner(client, "networks", project)
        if not project_is_visible(host, owner):
            raise HTTPException(status_code=403, detail="Access denied to this network")
        scope = project_params(owner)
        data = client.api.networks[safe_segment(network_name)].get(params=scope).json().get(
            "metadata"
        ) or {}

        # Same tenant-visibility gate as get_network: without this a Tenant on a
        # shared cluster could read the state of networks that aren't theirs.
        used_by = filter_visible_lxd_references(host, data.get("used_by") or [])
        if tenant_acl_active() and not used_by:
            raise HTTPException(status_code=403, detail="Access denied to this network")

        # jsonable_lxd, not the raw object: network.state() returns a pylxd AttributeDict that
        # pydantic cannot serialise, and the failure happens while rendering the response, after
        # this handler has returned, so the except blocks below never see it. The caller got a
        # bare 500 on a perfectly healthy cluster.
        state = client.api.networks[safe_segment(network_name)].state.get(params=scope).json().get(
            "metadata"
        ) or {}
        return {
            "name": network_name,
            "state": jsonable_lxd(state),
        }
    except HTTPException:
        raise
    except (LXDAPIException, ClientConnectionFailed, LXDConnectionError) as e:
        logger.error(f"LXD error getting state for network {network_name} on {host}: {e}")
        raise _lxd_http_error(e, network_name)
    except (requests.Timeout, requests.ConnectionError) as e:
        # important: a peer that accepted the connection and then stopped answering is not a
        # server fault. This fell into the blanket 500 below, and main.py replaces a 500 body
        # with an opaque reference, so a slow nuc2 reached the interface as an unactionable id.
        # Same taxonomy as the member resource read: 504 for a read that ran out of budget,
        # 503 for never getting a usable connection.
        logger.warning(f"Transport failure getting network state {network_name} on {host}: {e}")
        raise lxd_transport_error(e, host, f"reporting the state of {network_name}")
    except Exception as e:
        logger.error(f"Unexpected error getting network state {network_name}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get network state")
