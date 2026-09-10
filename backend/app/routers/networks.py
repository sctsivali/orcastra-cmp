"""
API Router for Network operations.
Provides endpoints for managing LXD networks.
"""

from fastapi import APIRouter, HTTPException, Depends, Query, Body
from typing import Optional, List, Dict, Literal
import logging
import requests
import asyncio
import re
from concurrent.futures import ThreadPoolExecutor

from app.services.lxd_service import (
    LXDService,
    LXDConnectionError,
    get_lxd_service,
)
from app.services.lxd.utils import jsonable_lxd
from pylxd.exceptions import LXDAPIException, ClientConnectionFailed
from app.core.dependencies import check_read_only_mode, require_tenant, require_partner, AuthenticatedUser
from app.core.org_scope import (
    org_scope_dependency,
    verify_cluster_access,
    get_scope_cache_key,
    project_is_visible,
)
from app.services.lxd.project_scope import (
    DEFAULT_PROJECT,
    list_across_projects,
    list_in_project,
    list_project_configs,
    project_params,
    resolve_owner,
)
from app.core.tenant_visibility import filter_visible_lxd_references, tenant_acl_active
from app.core.single_flight import single_flight
from app.core.cache import get_cache_manager
from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking
from pydantic import BaseModel, Field, field_validator, model_validator
from app.services.activity_logger import get_activity_logger

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
# Shed rather than queue: the pool is 5 wide and shared with the list fan-out.
NETWORK_MAX_IN_FLIGHT = 10


class NetworkInfo(BaseModel):
    """Network information."""
    name: str = Field(..., description="Network name")
    description: str = Field("", description="Network description")
    type: str = Field(..., description="Network type (bridge, physical, etc)")
    managed: bool = Field(True, description="Whether network is managed by LXD")
    config: Dict[str, str] = Field(default_factory=dict, description="Network configuration")
    used_by: List[str] = Field(default_factory=list, description="Instances using this network")
    status: str = Field("", description="Network status")
    node: str = Field("", description="Node name where network is defined")
    project: str = Field(
        DEFAULT_PROJECT,
        description="LXD project that holds this network",
    )


# Orcastra only supports creating bridge and OVN networks. The API has to enforce
# what the UI promises: `type`/`config` are handed straight to pylxd, so without a
# server-side allow-list a Partner could create any network type or set dangerous
# keys (raw.dnsmasq, bridge.external_interfaces, tunnel.*, ...) via the API.
ALLOWED_NETWORK_CONFIG_KEYS = {
    "ipv4.address",
    "ipv4.nat",
    "ipv6.address",
    "ipv6.nat",
    "network",  # OVN uplink
    "dns.domain",
}

# LXD network names must start with a letter (rejects a leading hyphen and
# all-digit names, both of which LXD refuses).
_NETWORK_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9-]*$")

# An OVN network reaches the outside world through an uplink, and LXD accepts only a managed
# Bridge or Physical network for the job. The dashboard used to hand the daemon whatever the
# operator picked, and the picker offered every interface it could see: other OVN networks,
# macvlan interfaces, and the loopback. On nuc1 today that is 11 candidates LXD cannot accept
# out of 23 offered. Choosing one produced a failure at the daemon, or an instance that never
# finished creating (BUG-ORC-006, and BUG-ORC-002 downstream of it).
OVN_UPLINK_TYPES = frozenset({"bridge", "physical"})


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
            f" Usable uplinks on this node: {', '.join(usable)}."
            if usable
            else " No network on this node can serve as an uplink; create a managed bridge or"
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
            detail=f"Uplink network '{uplink}' does not exist on this node." + _offer(),
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



def validate_network_config(config: Dict[str, str]) -> Dict[str, str]:
    """Reject config keys outside the supported set."""
    unsupported = sorted(k for k in config if k not in ALLOWED_NETWORK_CONFIG_KEYS)
    if unsupported:
        raise ValueError(
            "Unsupported network config key(s): "
            + ", ".join(unsupported)
            + ". Allowed: "
            + ", ".join(sorted(ALLOWED_NETWORK_CONFIG_KEYS))
        )
    return config


# Maps a daemon error onto a status and strips URLs and filesystem paths out of the text
# before it reaches the client.
#
# note: both used to be defined here, and this file was the only correct copy in the backend
# while 169 other handlers passed `str(e)` through untouched. They now live in app/core so the
# global 5xx handler and the image and storage-volume delete paths share one definition
# rather than growing variants that drift.
from app.core.lxd_errors import lxd_http_error, lxd_transport_error  # noqa: E402


def _is_clustered(client) -> bool:
    """True when the LXD host is a multi-member cluster.

    On a cluster, network creation is two-phase (define per member, then create
    cluster-wide). Unknown/unsupported -> treat as standalone (previous behavior).
    """
    try:
        meta = client.api.cluster.get().json().get("metadata", {}) or {}
        if not meta.get("enabled"):
            return False
        return len(_cluster_member_names(client)) > 1
    except Exception as e:
        logger.debug(f"Could not determine cluster state, assuming standalone: {e}")
        return False


def _cluster_member_names(client) -> List[str]:
    """Member names from /1.0/cluster/members (returns URLs at recursion=0)."""
    members = client.api.cluster.members.get().json().get("metadata", []) or []
    names = []
    for m in members:
        if isinstance(m, str):
            names.append(m.rstrip("/").rsplit("/", 1)[-1])
        elif isinstance(m, dict) and m.get("server_name"):
            names.append(m["server_name"])
    return names


def _lxd_http_error(exc: Exception, network_name: str) -> HTTPException:
    """Map an LXD/daemon error onto a meaningful status instead of a blanket 500."""
    # note: the mapping moved to app/core/lxd_errors so the image and storage-volume delete
    # paths share it. This wrapper stays because five call sites below read better naming
    # the network once rather than repeating the noun at each raise.
    return lxd_http_error(exc, "Network", network_name)


class NetworkCreateRequest(BaseModel):
    """Request body for creating a network."""
    name: str = Field(..., min_length=1, max_length=63, description="Network name")
    description: str = Field("", description="Network description")
    type: Literal["bridge", "ovn"] = Field("bridge", description="Network type (bridge or ovn)")
    config: Dict[str, str] = Field(default_factory=dict, description="Network configuration")

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not _NETWORK_NAME_RE.match(v):
            raise ValueError(
                "Name must start with a letter and contain only letters, numbers and hyphens"
            )
        return v

    @field_validator("config")
    @classmethod
    def _validate_config(cls, v: Dict[str, str]) -> Dict[str, str]:
        return validate_network_config(v)

    @model_validator(mode="after")
    def _validate_name_length_for_type(self):
        # Bridge network names become Linux interface names (IFNAMSIZ = 15).
        if self.type == "bridge" and len(self.name) > 15:
            raise ValueError("Bridge network names must be at most 15 characters")
        return self


class NetworkListResponse(BaseModel):
    """Response for listing networks."""
    total: int = Field(..., description="Total number of networks")
    networks: List[NetworkInfo] = Field(..., description="List of networks")
    nodes: List[str] = Field(..., description="List of nodes")
    unreachable_nodes: List[str] = Field(
        default_factory=list,
        description=(
            "Nodes that could not be reached. Their networks are missing from this "
            "list, so an unreachable node is distinguishable from one with none."
        ),
    )
    partial_nodes: List[str] = Field(
        default_factory=list,
        description=(
            "Nodes that answered but could not be read for every project, so their networks "
            "may be incomplete. A short list is never presented as the whole truth."
        ),
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
    description="Get a list of all networks from LXD nodes.",
)
@single_flight("networks:list")
async def list_networks(
    host: Optional[str] = Query(None, description="Filter by specific host node"),
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
    # Try cache first (60 second cache)
    cache = get_cache_manager()
    # important: the project belongs in the key. get_scope_cache_key mixes in the cluster
    # scope and the ACL digest but not the query, so without it a project-scoped read and a
    # fleet-wide one collide on one entry for the whole TTL.
    cache_key = get_scope_cache_key(f"networks:list:host={host}:project={project or '*'}")
    cached_data = None if fresh else cache.get("api_responses", cache_key)
    if cached_data is not None:
        logger.debug(f"Returning cached networks for host={host}")
        # Reconstruct Pydantic model from cached dict
        return NetworkListResponse(**cached_data)
    
    try:
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

        if tenant_acl_active():
            visible_networks = []
            for network in all_networks:
                network.used_by = filter_visible_lxd_references(network.node, network.used_by)
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
        )
        
        # important: an outage must not be remembered as the answer. See instances.py.
        if answered:
            cache.set("api_responses", cache_key, response.model_dump(), ttl=180)
            logger.debug("Cached networks list for 180 seconds")
        else:
            logger.warning("Not caching networks list: no node answered")

        return response
    except Exception as e:
        logger.error(f"Error listing networks: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{network_name}",
    response_model=NetworkInfo,
    summary="Get network details",
    description="Get detailed information about a specific network.",
)
async def get_network(
    network_name: str,
    host: str = Query(..., description="Target host node"),
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
            raise HTTPException(status_code=503, detail=f"Could not connect to {host}")
        owner = resolve_owner(client, "networks", project)
        if not project_is_visible(host, owner):
            raise HTTPException(status_code=403, detail="Access denied to this network")
        data = client.api.networks[network_name].get(
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
        raise HTTPException(status_code=503, detail=f"Could not reach LXD host '{host}'")
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
    host: str = Query(..., description="Target host node"),
    project: Optional[str] = Query(
        None, description="LXD project to create the network in"
    ),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Create a new network."""
    verify_cluster_access(host)

    def _create_blocking():
        """All the pylxd work for one create, in one hop off the loop.

        important: on a cluster this is N+4 sequential HTTP round trips (connect, cluster
        probe, member list, one define per member, the cluster-wide create, the read-back),
        so it scales with cluster size. Run inline it parks the event-loop thread for that
        whole sequence.
        """
        client = lxd_service.get_client_with_reconnect(host)
        if client is None:
            raise HTTPException(status_code=503, detail=f"Could not connect to {host}")

        # important: resolved, not taken as given. A project that does not set
        # features.networks holds no networks of its own and LXD stores this in default;
        # writing it as asked and reading it back as resolved would disagree about where it
        # went.
        project_configs = list_project_configs(client) if project else {}
        owner = resolve_owner(client, "networks", project)
        if not project_is_visible(host, owner):
            raise HTTPException(status_code=403, detail="Access denied to this project")

        validate_uplink(
            request,
            _uplink_candidates(client, host),
            project_configs.get(project or DEFAULT_PROJECT, {}),
        )
        scope = project_params(owner)

        if _is_clustered(client):
            # Clustered LXD: a network must first be DEFINED on every member
            # (POST ?target=<member>, member-scoped, no cluster-wide config) and
            # then CREATED cluster-wide (POST with no target). pylxd 2.4.1's
            # networks.create() has no target parameter, so drive the raw API.
            for member in _cluster_member_names(client):
                client.api.networks.post(
                    json={"name": request.name, "type": request.type},
                    params={**scope, "target": member},
                )
            client.api.networks.post(
                json={
                    "name": request.name,
                    "type": request.type,
                    "config": request.config,
                    "description": request.description,
                },
                params=scope,
            )
        else:
            client.api.networks.post(
                json={
                    "name": request.name,
                    "type": request.type,
                    "config": request.config,
                    "description": request.description,
                },
                params=scope,
            )
        created = client.api.networks[request.name].get(params=scope).json().get(
            "metadata"
        ) or {}
        created.setdefault("project", owner)
        return created

    try:
        try:
            network = await run_blocking(
                _create_blocking,
                timeout=NETWORK_CREATE_TIMEOUT,
                label=f"create_network[{host}/{request.name}]",
                executor=_executor,
                max_in_flight=NETWORK_MAX_IN_FLIGHT,
            )
        except BlockingTimeout:
            # The define/create fan-out may have partially applied; say so rather than
            # implying nothing happened.
            logger.error(f"create_network timed out on {host} for {request.name}")
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
        get_cache_manager().delete_pattern("api_responses", "networks:*")

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
    host: str = Query(..., description="Target host node"),
    project: Optional[str] = Query(
        None, description="LXD project holding the network (default project when omitted)"
    ),
    config: Optional[Dict[str, str]] = Body(None),
    description: Optional[str] = Body(None),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Update a network."""
    verify_cluster_access(host)
    # Same allow-list as create: config is passed straight to the daemon.
    if config is not None:
        try:
            validate_network_config(config)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    try:
        client = lxd_service.get_client_with_reconnect(host)
        if client is None:
            raise HTTPException(status_code=503, detail=f"Could not connect to {host}")
        owner = resolve_owner(client, "networks", project)
        if not project_is_visible(host, owner):
            raise HTTPException(status_code=403, detail="Access denied to this network")
        scope = project_params(owner)
        current = client.api.networks[network_name].get(params=scope).json().get(
            "metadata"
        ) or {}
        payload = {
            "config": current.get("config") or {},
            "description": current.get("description") or "",
        }
        if config is not None:
            payload["config"] = config
        if description is not None:
            payload["description"] = description
        client.api.networks[network_name].put(json=payload, params=scope)

        get_cache_manager().delete_pattern("api_responses", "networks:*")

        return {
            "success": True,
            "message": f"Network {network_name} updated successfully"
        }
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
    host: str = Query(..., description="Target host node"),
    project: Optional[str] = Query(
        None, description="LXD project holding the network (default project when omitted)"
    ),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Delete a network."""
    verify_cluster_access(host)
    try:
        client = lxd_service.get_client_with_reconnect(host)
        if client is None:
            raise HTTPException(status_code=503, detail=f"Could not connect to {host}")
        owner = resolve_owner(client, "networks", project)
        if not project_is_visible(host, owner):
            raise HTTPException(status_code=403, detail="Access denied to this network")
        client.api.networks[network_name].delete(params=project_params(owner))

        get_cache_manager().delete_pattern("api_responses", "networks:*")

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
    host: str = Query(..., description="Target host node"),
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
            raise HTTPException(status_code=503, detail=f"Could not connect to {host}")
        owner = resolve_owner(client, "networks", project)
        if not project_is_visible(host, owner):
            raise HTTPException(status_code=403, detail="Access denied to this network")
        scope = project_params(owner)
        data = client.api.networks[network_name].get(params=scope).json().get(
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
        state = client.api.networks[network_name].state.get(params=scope).json().get(
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
