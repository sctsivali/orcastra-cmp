"""
API Router for Network operations.
Provides endpoints for managing LXD networks.
"""

from fastapi import APIRouter, HTTPException, Depends, Query, Body
from typing import Optional, List, Dict, Literal
import logging
import asyncio
import re
from concurrent.futures import ThreadPoolExecutor

from app.services.lxd_service import (
    LXDService,
    LXDConnectionError,
    get_lxd_service,
)
from pylxd.exceptions import LXDAPIException, ClientConnectionFailed
from app.core.dependencies import check_read_only_mode, require_tenant, require_partner, AuthenticatedUser
from app.core.org_scope import org_scope_dependency, verify_cluster_access, get_scope_cache_key
from app.core.tenant_visibility import filter_visible_lxd_references, tenant_acl_active
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


# Strip URLs and filesystem paths out of daemon errors before they reach the
# client, so internal hostnames/paths aren't leaked to the UI.
_SENSITIVE_RE = re.compile(r"(https?://\S+|(?:/[\w.\-]+){2,})")


def _sanitize_error(message: str, limit: int = 300) -> str:
    return _SENSITIVE_RE.sub("[redacted]", message or "").strip()[:limit]


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
    msg = str(exc)
    low = msg.lower()
    if "not found" in low or "no such object" in low:
        return HTTPException(status_code=404, detail=f"Network '{network_name}' not found")
    if "already exists" in low:
        return HTTPException(status_code=409, detail=f"Network '{network_name}' already exists")
    if any(t in low for t in ("connect", "connection", "unreachable", "timed out", "timeout")):
        return HTTPException(status_code=503, detail="Could not reach the LXD host")
    if "in use" in low or "used by" in low:
        return HTTPException(status_code=409, detail=_sanitize_error(msg))
    # Remaining LXD errors are user-actionable validation failures.
    return HTTPException(status_code=400, detail=_sanitize_error(msg))


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


def _fetch_networks_for_node(
    lxd_service: LXDService, node_name: str
) -> tuple[str, Optional[List[NetworkInfo]]]:
    """Fetch networks for a single node - runs in thread pool.

    Returns (node_name, networks). `networks` is None when the node is
    unreachable, so the caller can tell "node is down" apart from "node has no
    networks" (previously both looked like an empty list).
    """
    networks_list: List[NetworkInfo] = []
    try:
        client = lxd_service.get_client_with_reconnect(node_name)
        if client is None:
            logger.warning(f"Could not connect to {node_name}, skipping for networks list")
            return node_name, None
        networks = client.networks.all()
        for network in networks:
            networks_list.append(NetworkInfo(
                name=network.name,
                description=getattr(network, 'description', '') or "",
                type=network.type,
                managed=network.managed,
                config=network.config or {},
                used_by=network.used_by or [],
                status=getattr(network, 'status', ''),
                node=node_name
            ))
    except Exception as e:
        logger.error(f"Error listing networks on {node_name}: {e}")
        return node_name, None
    return node_name, networks_list


@router.get(
    "",
    response_model=NetworkListResponse,
    summary="List all networks",
    description="Get a list of all networks from LXD nodes.",
)
async def list_networks(
    host: Optional[str] = Query(None, description="Filter by specific host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """List all networks."""
    if host:
        verify_cluster_access(host)
    # Try cache first (60 second cache)
    cache = get_cache_manager()
    cache_key = get_scope_cache_key(f"networks:list:host={host}")
    cached_data = cache.get("api_responses", cache_key)
    if cached_data is not None:
        logger.debug(f"Returning cached networks for host={host}")
        # Reconstruct Pydantic model from cached dict
        return NetworkListResponse(**cached_data)
    
    try:
        nodes_to_query = [host] if host else lxd_service.connection_manager.get_node_names()
        
        # Fetch networks from all nodes in parallel
        loop = asyncio.get_event_loop()
        tasks = [
            loop.run_in_executor(_executor, _fetch_networks_for_node, lxd_service, node_name)
            for node_name in nodes_to_query
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Flatten results, tracking nodes we could not reach so an unreachable
        # cluster isn't silently reported as "0 networks".
        all_networks = []
        unreachable_nodes: List[str] = []
        for result in results:
            if isinstance(result, tuple):
                node_name, networks = result
                if networks is None:
                    unreachable_nodes.append(node_name)
                else:
                    all_networks.extend(networks)
            elif isinstance(result, Exception):
                logger.error(f"Error fetching networks: {result}")

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
        )
        
        # Cache as dict for 120 seconds
        cache.set("api_responses", cache_key, response.model_dump(), ttl=180)
        logger.debug("Cached networks list for 120 seconds")
        
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
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get details for a specific network."""
    verify_cluster_access(host)
    try:
        client = lxd_service.get_client_with_reconnect(host)
        if client is None:
            raise HTTPException(status_code=503, detail=f"Could not connect to {host}")
        network = client.networks.get(network_name)
        used_by = filter_visible_lxd_references(host, network.used_by or [])
        if tenant_acl_active() and not used_by:
            raise HTTPException(status_code=403, detail="Access denied to this network")
        
        return NetworkInfo(
            name=network.name,
            description=getattr(network, 'description', '') or "",
            type=network.type,
            managed=network.managed,
            config=network.config or {},
            used_by=used_by,
            status=getattr(network, 'status', ''),
            node=host
        )
    except HTTPException:
        # 503 (unreachable) / 403 (tenant denied) must survive — previously the
        # generic handler collapsed everything into a 404 "not found", so a real
        # but unreachable network read as deleted.
        raise
    except (LXDAPIException, ClientConnectionFailed) as e:
        logger.error(f"LXD error getting network {network_name} on {host}: {e}")
        raise _lxd_http_error(e, network_name)
    except LXDConnectionError as e:
        logger.error(f"LXD connection error getting network {network_name}: {e}")
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

        if _is_clustered(client):
            # Clustered LXD: a network must first be DEFINED on every member
            # (POST ?target=<member>, member-scoped, no cluster-wide config) and
            # then CREATED cluster-wide (POST with no target). pylxd 2.4.1's
            # networks.create() has no target parameter, so drive the raw API.
            for member in _cluster_member_names(client):
                client.api.networks.post(
                    json={"name": request.name, "type": request.type},
                    params={"target": member},
                )
            client.api.networks.post(json={
                "name": request.name,
                "type": request.type,
                "config": request.config,
                "description": request.description,
            })
            return client.networks.get(request.name)
        # Standalone host: unchanged (this is the tested path).
        return client.networks.create(
            name=request.name,
            type=request.type,
            config=request.config,
            description=request.description
        )

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
                name=network.name,
                description=getattr(network, 'description', '') or "",
                type=network.type,
                managed=network.managed,
                config=network.config or {},
                used_by=network.used_by or [],
                status=getattr(network, 'status', ''),
                node=host
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
        network = client.networks.get(network_name)

        if config is not None:
            network.config = config
        if description is not None and hasattr(network, 'description'):
            network.description = description

        network.save()

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
        network = client.networks.get(network_name)
        network.delete()

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
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get network state."""
    verify_cluster_access(host)
    try:
        client = lxd_service.get_client_with_reconnect(host)
        if client is None:
            raise HTTPException(status_code=503, detail=f"Could not connect to {host}")
        network = client.networks.get(network_name)

        # Same tenant-visibility gate as get_network: without this a Tenant on a
        # shared cluster could read the state of networks that aren't theirs.
        used_by = filter_visible_lxd_references(host, network.used_by or [])
        if tenant_acl_active() and not used_by:
            raise HTTPException(status_code=403, detail="Access denied to this network")

        state = network.state()

        return {
            "name": network_name,
            "state": state
        }
    except HTTPException:
        raise
    except (LXDAPIException, ClientConnectionFailed, LXDConnectionError) as e:
        logger.error(f"LXD error getting state for network {network_name} on {host}: {e}")
        raise _lxd_http_error(e, network_name)
    except Exception as e:
        logger.error(f"Unexpected error getting network state {network_name}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get network state")
