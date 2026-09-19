"""
Instance/VM operations module.
Handles create, start, stop, restart, delete, and list operations for instances.
"""

import logging
import asyncio
from typing import Callable, List, Tuple, Optional, Dict, Any
from concurrent.futures import ThreadPoolExecutor
from pylxd.exceptions import LXDAPIException, ClientConnectionFailed
from requests import exceptions as requests_exc

from app.models.schemas import VMInfo
from app.services.lxd.nic_preflight import NicPreflightError, check_nics
from app.core.config import get_settings
from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking
from .project_limits import project_limit_from_lxd_error
from .exceptions import (
    LXDConnectionError,
    LXDLocalStorageError,
    LXDOperationError,
    LXDGracefulTimeoutError,
    LXDSnapshotExistsError,
    LXDProjectLimitError,
)
from .utils import (
    parse_vm_status,
    extract_ip_addresses,
    extract_ip_addresses_from_state,
    image_source_key,
    find_instance_in_projects,
    jsonable_lxd,
    normalize_cluster_member,
)
from .resources import ResourceManager
from .connection import LXDConnectionManager
from .project_scope import list_across_projects
from .audit import audit_operation
from app.core.lxd_path import safe_segment

logger = logging.getLogger(__name__)


def _announce_instance_created(host: str, project: str, name: str) -> None:
    """Tell OrcaHub an instance exists, once it actually does.

    important: paired with the add-on event above it and emitted for the same reason. The router
    announced this the moment LXD accepted the request, so a create LXD then refused still told
    OrcaHub an instance existed, with no correction to follow. On a cluster owned by two
    organisations it said so twice. Proven live: a create that failed with "Image not provided
    for instance creation" emitted instance.created to org 122 and org 6 seventy milliseconds
    later.

    Fire-and-forget by contract, so a webhook problem cannot fail a create that worked.
    """
    from app.services.orcahub_webhook import schedule_orcahub_event

    schedule_orcahub_event("instance.created", host, project, name)

# The pooled client is built for fast failure detection (3s connect / 8s read).
# That read timeout is shorter than the graceful-shutdown waits callers ask for,
# so the HTTP read would expire before LXD's own operation timeout -- making the
# caller's `timeout` argument meaningless and surfacing a raw connection error
# instead of a clean result. Stop/restart therefore use a client whose read
# timeout outlives the requested wait by this margin.
_GRACEFUL_READ_MARGIN_SECONDS = 30
# How long a start may hold the HTTP response open. The LXD operation itself is usually
# quick, but a loaded cluster is not, and the pooled client only allows an 8s read.
_START_WAIT_SECONDS = 30

# Hard ceiling on the wait a caller can request, mirroring VMActionRequest's
# `le=600` bound. Clamped again here so any internal caller that bypasses the
# request schema still can't hold an LXD operation request (and its worker) open
# for an unbounded time.
_MAX_ACTION_TIMEOUT_SECONDS = 600

# Connect leg for the raw rebuild calls. Kept separate from the (long) read leg because
# requests applies a scalar timeout to both, and nothing justifies a multi-minute budget for
# completing a TCP+TLS handshake.
_REBUILD_CONNECT_TIMEOUT = 5

# A rebuild holds its worker for as long as LXD takes to reinstall the root disk, up to
# wait_timeout (default 1800s). asyncio.to_thread would put that on the loop's DEFAULT
# executor, which is also where FastAPI runs every sync route handler -- a handful of
# concurrent rebuilds would starve unrelated requests. Own pool, and shed past it rather
# than queue.
# A delete tears down the root disk, which on a `dir` pool is a full recursive remove. The pooled
# client's 8 second read is far short of that, so the wait gets its own window.
_DELETE_WAIT_SECONDS = 300

_rebuild_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="lxd_rebuild")
_REBUILD_MAX_IN_FLIGHT = 8

# POST /vms is intentionally synchronous from the caller's perspective, but pylxd's
# connect/create(wait=True)/start/readback sequence is blocking. Keep it off the event loop and
# refuse excess work instead of building an unbounded 30-minute queue behind a slow image pull.
_CREATE_LXD_READ_TIMEOUT_SECONDS = 1800
_CREATE_WALL_TIMEOUT_SECONDS = (
    _CREATE_LXD_READ_TIMEOUT_SECONDS
    + _START_WAIT_SECONDS
    + _GRACEFUL_READ_MARGIN_SECONDS
)
_create_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="lxd_create")
_CREATE_MAX_IN_FLIGHT = 4


def _is_graceful_timeout(exc: Exception) -> bool:
    """True if `exc` means the guest never completed a graceful shutdown.

    Because `_rebind_for_wait` gives the client a read timeout of
    ``timeout + _GRACEFUL_READ_MARGIN_SECONDS``, the HTTP read always outlives
    LXD's own operation deadline, so the failure surfaces as LXD's message --
    'Failed shutting down instance, status is "Running": context deadline
    exceeded' -- and never as a raw HTTP read timeout. We therefore match only
    those two guest-side shapes. Matching the bare substring "timed out" would
    misclassify connection failures ('Connection to <ip> timed out', 'Connection
    pool timed out ...') as a guest ACPI failure and wrongly tell the operator to
    retry with force during a connectivity outage.
    """
    msg = str(exc).lower()
    return (
        "context deadline exceeded" in msg
        or "failed shutting down instance" in msg
    )


def _lxd_snapshot_names(client, name: str, project: str) -> List[str]:
    """List an instance's snapshot names via the raw LXD API.

    find_instance_in_projects returns an InstanceWrapper (raw-API backed), which
    has no pylxd `.snapshots`, so query GET /instances/{name}/snapshots directly.
    """
    try:
        resp = client.api.instances[safe_segment(name)].snapshots.get(params={"project": project})
        meta = resp.json().get("metadata") or []
        return [str(u).rstrip("/").rsplit("/", 1)[-1] for u in meta]
    except Exception as e:  # noqa: BLE001 - absent/uncreatable snapshots -> treat as none
        logger.warning("Could not list snapshots for %s: %s", name, e)
        return []

# Instance inventory and detail reads are synchronous all the way down: the cold path reads
# Vault, reconnects and validates a pylxd client, and the detail projection may call /state.
# Keep those paths off the event loop and give each one an explicit wall-clock and concurrency
# budget. The underlying transports already carry their socket bounds: Vault point reads use
# (2s, 3s), its registry LIST uses (3s, 10s), pooled LXD reads use (3s, 8s), and connection
# validation uses (3s, 5s). The wall budgets below outlive those sockets while still refusing a
# stalled or overloaded upstream promptly.
_INSTANCE_REGISTRY_TIMEOUT_SECONDS = 15.0
_INSTANCE_NODE_TIMEOUT_SECONDS = 30.0
_INSTANCE_DETAIL_TIMEOUT_SECONDS = 30.0
_INSTANCE_INVENTORY_WORKERS = 20
_INSTANCE_DETAIL_WORKERS = 8
_instance_inventory_executor = ThreadPoolExecutor(
    max_workers=_INSTANCE_INVENTORY_WORKERS,
    thread_name_prefix="instance_inventory_",
)
_instance_detail_executor = ThreadPoolExecutor(
    max_workers=_INSTANCE_DETAIL_WORKERS,
    thread_name_prefix="instance_detail_",
)

# Failed-node skip is delegated to the shared circuit breaker
# (app.services.lxd.health) so every connection path skips the same dead cluster.


# note: LXD reports the literal string "none" for an instance on a non-clustered target, not an
# absent value. Passing that through means the UI prints "none" in a Cluster member field, which
# reads as a member called none rather than as "this target is not clustered". Normalised once
# here so neither consumer has to know.
# Kept as a local name: the helper is shared with the GPU inventory (see utils).
_cluster_member = normalize_cluster_member


def _vm_info_from_row(row: Dict[str, Any], node_name: str, node_endpoint: str) -> VMInfo:
    """One record of an all-projects, recursion=2 instance list to VMInfo.

    `project` is what the daemon stamped on the row (list_across_projects refuses rows
    without it); `state` is inline, so the addresses come from it without a second call, and
    a stopped instance's empty state yields none. `resources` stays unset on the list path.
    """
    config_data = row.get("config") or {}
    expanded_config = row.get("expanded_config")
    expanded_devices = row.get("expanded_devices")
    tags_str = config_data.get("user.tags", "")
    tags = [t.strip() for t in tags_str.split(",") if t.strip()] if tags_str else []
    return VMInfo(
        name=row.get("name", ""),
        status=parse_vm_status(row.get("status", "Unknown"), row.get("status_code", 0)),
        status_code=row.get("status_code", 0),
        ip_addresses=extract_ip_addresses_from_state(row.get("state") or {}),
        host=node_name,
        host_endpoint=node_endpoint,
        architecture=row.get("architecture", ""),
        created_at=row.get("created_at"),
        last_used_at=row.get("last_used_at"),
        description=row.get("description", ""),
        type=row.get("type", "container"),
        resources=None,
        config=config_data,
        devices=row.get("devices") or {},
        expanded_devices=expanded_devices or {},
        effective_cpu_limit=(
            expanded_config.get("limits.cpu")
            if isinstance(expanded_config, dict)
            else None
        ),
        effective_memory_limit=(
            expanded_config.get("limits.memory")
            if isinstance(expanded_config, dict)
            else None
        ),
        effective_limits_known=isinstance(expanded_config, dict),
        expanded_devices_known=isinstance(expanded_devices, dict),
        profiles=row.get("profiles", []),
        tags=tags,
        project=row.get("project") or "default",
        location=_cluster_member(row.get("location")),
    )


# Config keys some LXD versions refuse outright for a given instance type, mapped to the first
# major version known to refuse them.
#
# `security.secureboot` is the one that matters and it is genuinely two-sided, which is why it
# is filtered here rather than defaulted away in the form:
#
#   * LXD 5.x accepts it, and community VM images (Alpine, the daily Ubuntu builds) are not
#     Secure Boot signed, so WITHOUT `security.secureboot=false` they create fine and then
#     refuse to start: "The image used by this instance is incompatible with secureboot".
#     Verified live on 5.21.7. Only signed release images carry
#     `image.requirements.secureboot=false` and cope on their own.
#   * LXD 6.9 answers `"security.secureboot" is not supported for "virtual-machine"` and
#     refuses the create outright.
#
# So neither always-send nor never-send is correct, and the right answer depends on the target
# cluster. That is knowledge about the cluster, so it lives here, next to the client that knows
# which cluster it is, rather than in every caller that builds a config.
_KEYS_REFUSED_FROM_MAJOR = {
    "security.secureboot": 6,
}


def _server_major(client) -> Optional[int]:
    """Major version of the LXD this client is talking to, or None when unknown.

    pylxd caches the /1.0 body on the client as host_info, and _validate_connection refreshes
    it on every pooled revalidation, so this is free.
    """
    try:
        version = ((getattr(client, "host_info", None) or {})
                   .get("environment", {})
                   .get("server_version", ""))
        return int(str(version).split(".", 1)[0])
    except (AttributeError, IndexError, TypeError, ValueError):
        return None


def _drop_unsupported_config(client, instance_type: str, config: dict) -> dict:
    """Remove config keys the target LXD is known to refuse, leaving everything else alone.

    important: fail-safe direction is to KEEP the key. An unknown or unparseable version
    behaves like 5.x, which is the behaviour verified against a live cluster; dropping on
    uncertainty would silently reintroduce the unbootable-community-image case.
    """
    if instance_type == "container" or not config:
        return config
    major = _server_major(client)
    if major is None:
        return config
    kept = {}
    for key, value in config.items():
        refused_from = _KEYS_REFUSED_FROM_MAJOR.get(key)
        if refused_from is not None and major >= refused_from:
            logger.info(
                "Dropping %s: LXD %s refuses it for a virtual-machine, and sending it fails "
                "the create outright.", key, major,
            )
            continue
        kept[key] = value
    return kept


class InstanceManager:
    """
    Manages LXD instance operations.
    """
    
    def __init__(self, connection_manager: LXDConnectionManager, resource_manager: ResourceManager, cert_manager=None):
        self.connection_manager = connection_manager
        self.resource_manager = resource_manager
        self.cert_manager = cert_manager
        # Strong refs to fire-and-forget background tasks (e.g. auto-start after
        # create) so the event loop doesn't garbage-collect them mid-flight.
        self._bg_tasks: set = set()
    
    def _get_client_with_reconnect(self, node_name: str):
        """Get LXD client with automatic reconnection if stale.
        Respects org-scope: returns None for clusters outside user's org.
        """
        # Org-scope guard: prevent reconnecting to clusters outside the user's org
        from app.core.org_scope import get_current_allowed_clusters
        allowed = get_current_allowed_clusters()
        if allowed is not None and node_name not in allowed:
            return None

        client = self.connection_manager.get_client(node_name)
        
        # If client is None (stale connection), try to reconnect via cert_manager
        if client is None and self.cert_manager:
            logger.info(f"Connection to {node_name} is stale, attempting reconnect...")
            try:
                # Pass connection_manager for proper pooling
                client = self.cert_manager.connect_with_vault_cert(
                    node_name, 
                    connection_manager=self.connection_manager
                )
                logger.info(f"Successfully reconnected to {node_name}")
            except Exception as e:
                logger.warning(f"Reconnect failed for {node_name}: {e}")
                client = None
        
        return client
    
    def _get_node_endpoint(self, node_name: str) -> str:
        """Get node endpoint from Vault metadata with caching."""
        try:
            settings = get_settings()
            if self.cert_manager and self.cert_manager.vault:
                # Cache is automatically used in vault_read
                metadata = self.cert_manager.vault.vault_read(
                    f"{settings.vault_clusters_path}/{node_name}/metadata",
                    use_cache=True  # Explicitly enable cache
                )
                return metadata.get("endpoint", "unknown")
        except Exception as e:
            logger.warning(f"Failed to get endpoint for {node_name} from Vault: {e}")
        return "unknown"
    
    def _instance_to_vm_info(
        self, 
        instance, 
        node_name: str, 
        node_endpoint: str
    ) -> VMInfo:
        """Convert pylxd instance to VMInfo model."""
        # Parse tags from config
        tags_str = instance.config.get("user.tags", "")
        tags = [tag.strip() for tag in tags_str.split(",") if tag.strip()] if tags_str else []
        
        return VMInfo(
            name=instance.name,
            status=parse_vm_status(instance.status, instance.status_code),
            status_code=instance.status_code,
            ip_addresses=extract_ip_addresses(instance),
            host=node_name,
            host_endpoint=node_endpoint,
            project=getattr(instance, 'project', 'default'),
            architecture=instance.architecture,
            created_at=instance.created_at,
            last_used_at=instance.last_used_at,
            description=instance.description,
            type=instance.type,
            resources=self.resource_manager.get_resource_usage(instance),
            config=dict(instance.config) if instance.config else None,
            devices=jsonable_lxd(getattr(instance, "devices", None)) or {},
            expanded_devices=jsonable_lxd(getattr(instance, "expanded_devices", None)) or {},
            effective_cpu_limit=(
                (getattr(instance, "expanded_config", None) or {}).get("limits.cpu")
            ),
            effective_memory_limit=(
                (getattr(instance, "expanded_config", None) or {}).get("limits.memory")
            ),
            effective_limits_known=isinstance(
                getattr(instance, "expanded_config", None), dict
            ),
            expanded_devices_known=isinstance(
                getattr(instance, "expanded_devices", None), dict
            ),
            profiles=list(instance.profiles) if instance.profiles else [],
            tags=tags,
            location=_cluster_member(getattr(instance, "location", None)),
        )
    
    async def list_all_vms(self) -> Tuple[List[VMInfo], List[str]]:
        """List all instances while preserving the established two-value contract."""
        vms, connected, _incomplete = await self.list_all_vms_with_failures()
        return vms, connected

    def _inventory_node_names_blocking(self) -> List[str]:
        """Read the fleet registry from Vault, with the process registry as fallback."""
        if self.cert_manager:
            try:
                node_names = self.cert_manager.list_clusters()
                logger.debug("Found %s clusters in Vault: %s", len(node_names), node_names)
                return node_names
            except Exception as exc:
                logger.warning("Could not list Vault clusters: %s", exc)
        return self.connection_manager.get_node_names()

    def _fetch_inventory_node(
        self,
        node_name: str,
    ) -> Tuple[List[VMInfo], bool, bool]:
        """Read endpoint metadata and instance state in the same bounded worker."""
        endpoint = self._get_node_endpoint(node_name)
        return self._fetch_vms_from_node_detailed(node_name, endpoint)

    async def _fetch_inventory_node_bounded(
        self,
        node_name: str,
    ) -> Tuple[List[VMInfo], bool, bool]:
        return await run_blocking(
            self._fetch_inventory_node,
            node_name,
            timeout=_INSTANCE_NODE_TIMEOUT_SECONDS,
            label=f"instance.inventory[{node_name}]",
            executor=_instance_inventory_executor,
            max_in_flight=_INSTANCE_INVENTORY_WORKERS,
        )

    async def list_all_vms_with_failures(
        self,
    ) -> Tuple[List[VMInfo], List[str], List[str]]:
        """
        List all VMs across all configured LXD nodes and all projects.
        Returns (vm_list, connected_nodes, incomplete_nodes). ``incomplete_nodes`` names every
        registered cluster that did not produce a complete upstream answer, including
        unreachable clusters, so callers do not need a second synchronous Vault read. Local
        executor timeout or saturation is raised as service unavailability instead.
        """
        try:
            node_names = await run_blocking(
                self._inventory_node_names_blocking,
                timeout=_INSTANCE_REGISTRY_TIMEOUT_SECONDS,
                label="instance.inventory.registry",
                executor=_instance_inventory_executor,
                max_in_flight=_INSTANCE_INVENTORY_WORKERS,
            )
        except (BlockingTimeout, BlockingPoolFull) as exc:
            raise LXDConnectionError(
                "Instance inventory is temporarily unavailable"
            ) from exc

        from app.core.org_scope import apply_cluster_filter
        node_names = apply_cluster_filter(node_names)
        
        if not node_names:
            logger.warning("No nodes found in Vault or connection manager")
            return [], [], []

        # Aggregate results
        all_vms: List[VMInfo] = []
        connected_nodes: List[str] = []
        incomplete_nodes: List[str] = []
        capacity_errors: List[Exception] = []

        # One request must not saturate its own executor when the fleet has more clusters than
        # workers. Batches retain parallel fan-out while leaving the global in-flight cap honest
        # across concurrent organization scopes.
        for start in range(0, len(node_names), _INSTANCE_INVENTORY_WORKERS):
            batch = node_names[start : start + _INSTANCE_INVENTORY_WORKERS]
            results = await asyncio.gather(
                *(self._fetch_inventory_node_bounded(node_name) for node_name in batch),
                return_exceptions=True,
            )
            for node_name, result in zip(batch, results):
                if isinstance(result, Exception):
                    logger.warning("Error fetching VMs from %s: %s", node_name, result)
                    incomplete_nodes.append(node_name)
                    if isinstance(result, (BlockingTimeout, BlockingPoolFull)):
                        capacity_errors.append(result)
                    continue

                vms, connected, complete = result
                if connected:
                    connected_nodes.append(node_name)
                    all_vms.extend(vms)
                if not connected or not complete:
                    incomplete_nodes.append(node_name)

        # Ordinary cluster failures return (connected=False) and remain a useful partial answer.
        # A wall timeout or saturated executor means this backend could not finish the bounded
        # read at all; do not cache that capacity incident as if the named cluster were offline.
        if capacity_errors:
            raise LXDConnectionError(
                "Instance inventory is temporarily unavailable"
            ) from capacity_errors[0]

        logger.info(f"Fetched {len(all_vms)} VMs from {len(connected_nodes)} nodes")
        return all_vms, connected_nodes, incomplete_nodes
    
    def fetch_node_instances(
        self, node_name: str, node_endpoint: str = ""
    ) -> Tuple[List[VMInfo], bool]:
        """One node's instances across every project, for a caller that must not fan out.

        important: this exists so the API-key surface can read instances without going through
        ``list_all_vms``. An API-key request establishes no org scope, so
        ``get_current_allowed_clusters()`` returns None and the cluster filter inside
        ``get_client`` matches everything. A fleet-wide call from that context would therefore
        reach past the key's own grant. Callers there iterate the grant and ask for one node at
        a time, which is what this is.

        ``node_endpoint`` only fills ``VMInfo.host_endpoint``; it has no effect on the fetch.
        """
        vms, connected, _complete = self._fetch_vms_from_node_detailed(
            node_name,
            node_endpoint,
        )
        return vms, connected

    def _fetch_vms_from_node(
        self,
        node_name: str,
        node_endpoint: str,
    ) -> Tuple[List[VMInfo], bool]:
        """Compatibility wrapper for callers that do not consume partial-read detail."""
        vms, connected, _complete = self._fetch_vms_from_node_detailed(
            node_name,
            node_endpoint,
        )
        return vms, connected

    def _fetch_vms_from_node_detailed(
        self,
        node_name: str,
        node_endpoint: str,
    ) -> Tuple[List[VMInfo], bool, bool]:
        """
        Fetch all VMs from a single node (synchronous, for thread pool execution).
        Returns (vm_list, is_connected, is_complete).
        """
        from app.services.lxd import health
        vms: List[VMInfo] = []
        complete = True

        # Skip clusters the shared breaker marked as recently failed.
        if health.is_tripped(node_name):
            logger.debug(f"Skipping {node_name} - recently failed (cached)")
            return [], False, False

        try:
            # Try to get or reconnect client
            client = self.connection_manager.get_client(node_name)
            
            if client is None and self.cert_manager:
                try:
                    cluster_info = self.cert_manager.get_cluster_info(node_name)
                    cert_data = self.cert_manager.get_cluster_certificate(node_name)
                    
                    client = self.connection_manager.connect_with_vault_cert(
                        cluster_id=node_name,
                        endpoint=cluster_info["metadata"]["endpoint"],
                        cert_content=cert_data["client.crt"],
                        key_content=cert_data["client.key"],
                        verify=False
                    )
                    logger.info(f"Reconnected to {node_name}")
                except LXDLocalStorageError:
                    # See lxd_service.ensure_connection: the backend's own storage failed, so
                    # recording it here would mark a healthy node offline for the breaker TTL.
                    raise
                except Exception as e:
                    logger.warning(f"Reconnect failed for {node_name}: {e}")
                    # Preserve a more specific diagnosis. connect_with_vault_cert records
                    # KIND_UNAUTHORIZED for a host that answered and refused us, and a generic
                    # overwrite here would make it read as powered off everywhere.
                    if health.get_kind(node_name) is None:
                        health.record_failure(node_name, str(e))
                    return [], False, False
            
            if client is None:
                # important: do NOT trip the breaker on a missing client. connection.py's
                # _auto_reconnect already refuses to, and says why: an empty in-process cache is
                # the normal state for a cluster registered after this worker booted, and
                # tripping there "seals the only doors that could reopen it". Every path that
                # DID fail records its own kind before returning None, so the `get_kind is None`
                # guard that used to sit here fired only in the case that must never trip: a
                # cluster this worker has simply not connected to yet, or one the caller's org
                # scope excluded. The effect was a freshly registered cluster reading offline
                # across the product for a full breaker TTL, with no log line anywhere.
                logger.debug(
                    "No client for %s in this worker; reporting unreachable without tripping "
                    "the breaker", node_name
                )
                return [], False, False
            
            # One request for every project's instances, state included (recursion=2), on a
            # daemon that advertises instance_all_projects; list_across_projects walks the
            # projects one by one otherwise. The per-project walk plus one /state call per
            # running instance took 11 s on a 52-project host; this is well under a second.
            try:
                rows, complete = list_across_projects(client, "instances", recursion=2)
            except requests_exc.ReadTimeout:
                # A large partner cloud can outrun the read budget with state attached.
                # Listing without addresses beats reporting the cluster unreachable.
                logger.warning(
                    "Instance list with state on %s exceeded the read budget; re-reading "
                    "without state, addresses omitted", node_name,
                )
                rows, complete = list_across_projects(client, "instances", recursion=1)
            if not complete:
                logger.warning(
                    "Instance list on %s is partial: not every project could be read", node_name
                )
            for row in rows:
                try:
                    vms.append(_vm_info_from_row(row, node_name, node_endpoint))
                except Exception as e:
                    logger.error(
                        "Error processing instance %s/%s on %s: %s",
                        row.get("project"), row.get("name"), node_name, e,
                    )
                    complete = False
            return vms, True, complete
            
        except Exception as e:
            logger.warning(f"Error fetching VMs from {node_name}: {e}")
            return [], False, False
    
    async def get_vm(
        self,
        name: str,
        host: str,
        project: Optional[str] = None,
        project_filter: Optional[Callable[[str], bool]] = None,
    ) -> Optional[VMInfo]:
        """Get a specific VM by name and host.

        important: an instance name is unique per project, not per host. Without `project`,
        callers at an authorization boundary must pass `project_filter` so only visible projects
        are searched. Internal trusted callers may omit the filter when they intentionally need
        the legacy cross-project lookup.
        """
        try:
            return await run_blocking(
                self._get_vm_blocking,
                name,
                host,
                project,
                project_filter,
                timeout=_INSTANCE_DETAIL_TIMEOUT_SECONDS,
                label=f"instance.detail[{host}/{project or '*'}]",
                executor=_instance_detail_executor,
                max_in_flight=_INSTANCE_DETAIL_WORKERS,
            )
        except (BlockingTimeout, BlockingPoolFull) as exc:
            raise LXDConnectionError(
                "Instance detail is temporarily unavailable"
            ) from exc

    def _get_vm_blocking(
        self,
        name: str,
        host: str,
        project: Optional[str] = None,
        project_filter: Optional[Callable[[str], bool]] = None,
    ) -> Optional[VMInfo]:
        """Run reconnect, project lookup, state reads, and projection in one worker."""
        try:
            client = self._get_client_with_reconnect(host)
            if client is None:
                raise LXDConnectionError(f"Could not connect to {host}")
            endpoint = self._get_node_endpoint(host)

            if project_filter is None:
                instance, project_name = find_instance_in_projects(client, name, project)
            else:
                instance, project_name = find_instance_in_projects(
                    client,
                    name,
                    project,
                    project_filter=project_filter,
                )
            
            if instance is None:
                return None
            
            vm_info = self._instance_to_vm_info(instance, host, endpoint)
            # important: the resolved project goes on the model, not into the config map. It used
            # to be written as a synthetic config key that nothing reads, while VMInfo.project kept
            # the bound client's project. So every single-instance read of an instance outside
            # `default` answered project="default", and callers that trust the field (the detail
            # dialog's Snapshots tab, and the tenant ACL check in the instances router) acted on
            # the wrong project. The list path has always set it correctly, which is why only the
            # detail reads were affected.
            vm_info.project = project_name
            
            return vm_info
            
        except LXDConnectionError:
            raise
        except (requests_exc.RequestException, ClientConnectionFailed) as exc:
            raise LXDConnectionError(
                f"Could not read instance {name} from {host}"
            ) from exc
        except Exception as e:
            logger.error(f"Error getting VM {name} from {host}: {e}")
            raise LXDOperationError(f"Failed to get VM: {str(e)}")
    
    @audit_operation("lxd.instance.start", "instance")
    async def start_vm(
        self,
        name: str,
        host: str,
        force: bool = False,
        project: Optional[str] = None,
    ) -> VMInfo:
        """Start a VM (searches all projects)."""
        try:
            client = self._get_client_with_reconnect(host)
            if client is None:
                raise LXDConnectionError(f"Could not connect to {host}")
            endpoint = self._get_node_endpoint(host)
            
            instance, project_name = find_instance_in_projects(client, name, project)
            
            if instance is None:
                raise LXDOperationError(f"VM {name} not found on {host}")
            
            if instance.status == "Running":
                vm_info = self._instance_to_vm_info(instance, host, endpoint)
            elif instance.status == "Frozen":
                raise LXDOperationError(
                    f"Instance {name} is frozen by billing policy and cannot be started. "
                    f"Please reactivate the subscription through the billing portal."
                )
            else:
                started = await asyncio.to_thread(
                    self._start_and_wait, instance, host, project_name
                )
                logger.info(f"Started VM {name} on {host} (project: {project_name})")
                vm_info = self._instance_to_vm_info(started, host, endpoint)
            
            # important: the resolved project goes on the model, not into the config map. It used
            # to be written as a synthetic config key that nothing reads, while VMInfo.project kept
            # the bound client's project. So every single-instance read of an instance outside
            # `default` answered project="default", and callers that trust the field (the detail
            # dialog's Snapshots tab, and the tenant ACL check in the instances router) acted on
            # the wrong project. The list path has always set it correctly, which is why only the
            # detail reads were affected.
            vm_info.project = project_name
            
            return vm_info
            
        except LXDAPIException as e:
            raise LXDOperationError(f"Failed to start VM: {str(e)}")
        except (requests_exc.ConnectionError, requests_exc.Timeout) as e:
            # important: the cluster stopped answering part-way through the start. That is an
            # upstream availability problem the caller can retry, not an internal fault, and
            # without this it reached the router's `except Exception` and became a 500 with a
            # reference id. Observed live: a read timeout at 8.9s while the instance went on
            # to reach Running, so the operator was told the server broke about an action
            # that had in fact succeeded.
            raise LXDConnectionError(
                f"Lost the connection to {host} while starting {name}: {e}"
            )
    
    def _rebind_for_wait(self, instance, host: str, project_name: str, timeout: int):
        """Re-fetch `instance` on a client whose read timeout outlives `timeout`.

        Returns the original instance unchanged if such a client can't be built,
        so a graceful stop/restart degrades to the old behaviour rather than
        failing outright.
        """
        try:
            waiting_client = self.connection_manager.get_project_client(
                host, project_name, (5, timeout + _GRACEFUL_READ_MARGIN_SECONDS)
            )
            if waiting_client is not None:
                return waiting_client.instances.get(safe_segment(instance.name))
        except Exception as e:  # noqa: BLE001 - fall back to the pooled client
            logger.warning(
                "Could not build a long-timeout client for %s on %s: %s", instance.name, host, e
            )
        return instance

    def _start_and_wait(self, instance, host: str, project_name: str):
        """Start `instance` on a client whose read timeout outlives the start.

        important: the pooled client allows an 8s read (DEFAULT_LXD_TIMEOUT) and
        `start(wait=True)` holds the HTTP response open for the whole LXD operation. A loaded
        cluster takes longer than that, so the READ timed out while the instance went on
        starting, and the caller reported a 500 for an action that had succeeded. Observed
        live at 8.9s against a busy cluster, with the VM reaching Running anyway.
        `stop`/`restart` already avoid this through `_rebind_for_wait`; start did not, which
        is the asymmetry this closes. Falls back to the pooled client when no long-timeout
        client can be built, so the worst case is the old behaviour.
        """
        target = self._rebind_for_wait(instance, host, project_name, _START_WAIT_SECONDS)
        target.start(wait=True)
        target.sync()
        return target

    @staticmethod
    def _graceful_timeout(name: str, action: str, timeout: int) -> LXDGracefulTimeoutError:
        # A graceful stop only works if software inside the guest acts on the ACPI
        # power event. A fully-booted OS does so in about a second; the cases below
        # never will, no matter how long the timeout.
        # note: the remedy comes before the causes, and the whole message fits the 300
        # characters a 5xx detail is cut to on the way out (core/error_sanitizer.py) with the
        # longest legal name (63) and timeout (600). The old order lost "retry with force" to
        # the cut on any name longer than a few characters.
        return LXDGracefulTimeoutError(
            f"Timed out after {timeout}s waiting for '{name}' to shut down gracefully: "
            f"nothing in the guest acted on the ACPI request. Retry the {action} with "
            f"force to power-cycle it. Causes: no OS, a boot menu or UEFI shell, early "
            f"boot, an OS ignoring ACPI."
        )

    @audit_operation("lxd.instance.stop", "instance")
    async def stop_vm(
        self,
        name: str,
        host: str,
        force: bool = False,
        timeout: int = 30,
        project: Optional[str] = None,
    ) -> VMInfo:
        """Stop a VM (searches all projects)."""
        timeout = max(1, min(timeout, _MAX_ACTION_TIMEOUT_SECONDS))
        try:
            client = self._get_client_with_reconnect(host)
            if client is None:
                raise LXDConnectionError(f"Could not connect to {host}")
            endpoint = self._get_node_endpoint(host)

            instance, project_name = find_instance_in_projects(client, name, project)

            if instance is None:
                raise LXDOperationError(f"VM {name} not found on {host}")

            if instance.status == "Stopped":
                vm_info = self._instance_to_vm_info(instance, host, endpoint)
            else:
                # A graceful stop blocks on the guest for up to `timeout`s. Those
                # pylxd calls are synchronous, so run them in a worker thread to
                # avoid stalling the event loop for the whole wait (a blank VM
                # burns the full timeout since nothing answers ACPI). A forced
                # stop is immediate but goes through the same path for uniformity.
                def _do_stop():
                    target = instance if force else self._rebind_for_wait(instance, host, project_name, timeout)
                    try:
                        target.stop(force=force, wait=True, timeout=timeout)
                    except Exception as e:
                        if not force and _is_graceful_timeout(e):
                            raise self._graceful_timeout(name, "stop", timeout) from e
                        raise
                    target.sync()
                    return target

                target = await asyncio.to_thread(_do_stop)
                logger.info(f"Stopped VM {name} on {host} (project: {project_name})")
                vm_info = self._instance_to_vm_info(target, host, endpoint)

            # important: the resolved project goes on the model, not into the config map. It used
            # to be written as a synthetic config key that nothing reads, while VMInfo.project kept
            # the bound client's project. So every single-instance read of an instance outside
            # `default` answered project="default", and callers that trust the field (the detail
            # dialog's Snapshots tab, and the tenant ACL check in the instances router) acted on
            # the wrong project. The list path has always set it correctly, which is why only the
            # detail reads were affected.
            vm_info.project = project_name

            return vm_info

        except LXDAPIException as e:
            raise LXDOperationError(f"Failed to stop VM: {str(e)}")
    
    @audit_operation("lxd.instance.restart", "instance")
    async def restart_vm(
        self, 
        name: str, 
        host: str, 
        force: bool = False,
        timeout: int = 30,
        project: Optional[str] = None,
    ) -> VMInfo:
        """Restart a VM (searches all projects)."""
        timeout = max(1, min(timeout, _MAX_ACTION_TIMEOUT_SECONDS))
        try:
            client = self._get_client_with_reconnect(host)
            if client is None:
                raise LXDConnectionError(f"Could not connect to {host}")
            endpoint = self._get_node_endpoint(host)

            instance, project_name = find_instance_in_projects(client, name, project)

            if instance is None:
                raise LXDOperationError(f"VM {name} not found on {host}")

            if instance.status == "Frozen":
                raise LXDOperationError(
                    f"Instance {name} is frozen by billing policy and cannot be restarted. "
                    f"Please reactivate the subscription through the billing portal."
                )

            # The graceful restart blocks on the guest shutdown for up to
            # `timeout`s; run the synchronous pylxd calls in a worker thread so the
            # event loop isn't stalled for the full wait. A forced restart is a
            # power-cycle but goes through the same path for uniformity.
            def _do_restart():
                target = instance if force else self._rebind_for_wait(instance, host, project_name, timeout)
                try:
                    target.restart(force=force, wait=True, timeout=timeout)
                except Exception as e:
                    if not force and _is_graceful_timeout(e):
                        raise self._graceful_timeout(name, "restart", timeout) from e
                    raise
                target.sync()
                return target

            target = await asyncio.to_thread(_do_restart)

            logger.info(f"Restarted VM {name} on {host} (project: {project_name})")
            vm_info = self._instance_to_vm_info(target, host, endpoint)

            # important: the resolved project goes on the model, not into the config map. It used
            # to be written as a synthetic config key that nothing reads, while VMInfo.project kept
            # the bound client's project. So every single-instance read of an instance outside
            # `default` answered project="default", and callers that trust the field (the detail
            # dialog's Snapshots tab, and the tenant ACL check in the instances router) acted on
            # the wrong project. The list path has always set it correctly, which is why only the
            # detail reads were affected.
            vm_info.project = project_name

            return vm_info

        except LXDAPIException as e:
            raise LXDOperationError(f"Failed to restart VM: {str(e)}")
    
    @audit_operation("lxd.instance.delete", "instance")
    async def delete_vm(
        self, 
        name: str, 
        host: str, 
        force: bool = False,
        project: Optional[str] = None,
    ) -> str:
        """Delete a VM, returning the project it was actually deleted from.

        important: the RESOLVED project, not the requested one. Callers stamp tags, webhooks,
        upstream events and the audit trail with it, and a caller that passed no project was
        stamping "default" for an instance deleted from somewhere else entirely, so the audit
        trail named a project the deletion never touched.
        """
        try:
            client = self._get_client_with_reconnect(host)
            if client is None:
                raise LXDConnectionError(f"Could not connect to {host}")
            
            instance, project_name = find_instance_in_projects(client, name, project)
            
            if instance is None:
                raise LXDOperationError(f"VM {name} not found on {host}")
            
            # important: both waits run OFF the event loop and on a client whose socket window
            # outlives them. The pooled client reads for 8 seconds; tearing down a root disk on a
            # `dir` pool, or on a loaded host, takes longer. The read then died first, the
            # ReadTimeout surfaced as LXDOperationError and the caller was answered 500 for a
            # delete LXD had in fact completed, so the operator saw a server error and an instance
            # that was already gone. Same rule as stop and restart beside it, and the same rule the
            # snapshot wait states: a wait has to outlast what it is waiting for.
            def _do_delete():
                target = self._rebind_for_wait(instance, host, project_name, _DELETE_WAIT_SECONDS)
                if target.status in ("Running", "Frozen"):
                    target.stop(force=True, wait=True)
                target.delete(wait=True)

            # Stop an active instance before deleting when force is set. A Frozen
            # instance (billing-paused) is still active, so LXD refuses to delete
            # it until it is stopped -- force-stop covers both Running and Frozen.
            if instance.status in ("Running", "Frozen") and not force:
                raise LXDOperationError(
                    f"VM {name} is {instance.status.lower()}. Stop it first or use force=true"
                )

            await asyncio.to_thread(_do_delete)

            logger.info(f"Deleted VM {name} from {host} (project: {project_name})")
            return project_name
            
        except LXDAPIException as e:
            raise LXDOperationError(f"Failed to delete VM: {str(e)}")

    async def get_rebuild_preflight(self, name: str, host: str, project: Optional[str] = None) -> Dict[str, Any]:
        """Gather what the rebuild flow needs before it starts: the instance status,
        its snapshots (the block condition -- LXD refuses to rebuild an instance that
        has any), and a snapshot of the config + attached custom volumes for the job
        record. Blocking pylxd calls run off the event loop.
        """
        def _work() -> Dict[str, Any]:
            client = self._get_client_with_reconnect(host)
            if client is None:
                raise LXDConnectionError(f"Could not connect to {host}")
            instance, project_name = find_instance_in_projects(client, name, project)
            if instance is None:
                raise LXDOperationError(f"Instance {name} not found on {host}")
            snapshots = _lxd_snapshot_names(client, name, project_name)
            data = instance._data or {}
            config = dict(data.get("config") or {})
            devices = dict(data.get("expanded_devices") or data.get("devices") or {})
            # Attached custom volumes survive a rebuild: disk devices other than the
            # root that reference a pool volume by source.
            attached = {
                dname: dcfg for dname, dcfg in devices.items()
                if dcfg.get("type") == "disk" and dname != "root" and dcfg.get("source")
            }
            base_image = config.get("image.description") or config.get("volatile.base_image")
            return {
                "project": project_name,
                "status": instance.status,
                "type": instance.type,
                "snapshots": snapshots,
                "config": config,
                "attached_volumes": attached,
                "base_image": base_image,
            }
        return await asyncio.to_thread(_work)

    async def rebuild_instance(
        self,
        name: str,
        host: str,
        source: Dict[str, Any],
        project: Optional[str] = None,
        cloud_init: Optional[str] = None,
        wait_timeout: int = 1800,
    ) -> str:
        """Rebuild the instance's root disk from `source` (LXD `POST .../rebuild`).

        Reinstalls the root disk while LXD preserves the instance identity, config
        and attached volumes. The instance must already be stopped and must have no
        snapshots (caller enforces both). Optionally sets cloud-init.user-data first
        so the fresh root disk picks it up on first boot. pylxd has no rebuild
        helper, so this drives the raw API and waits on the returned operation.
        Returns the LXD operation id.
        """
        client = self._get_client_with_reconnect(host)
        if client is None:
            raise LXDConnectionError(f"Could not connect to {host}")
        instance, project_name = find_instance_in_projects(client, name, project)
        if instance is None:
            raise LXDOperationError(f"Instance {name} not found on {host}")

        # Snapshot guard: LXD rejects a rebuild if any snapshot exists. Surface it
        # as a typed error the router maps to a clear 409 listing the snapshots.
        snapshots = await asyncio.to_thread(_lxd_snapshot_names, client, name, project_name)
        if snapshots:
            raise LXDSnapshotExistsError(snapshots)

        if cloud_init is not None:
            # PATCH merges config, so this only sets cloud-init.user-data. The new
            # root disk picks it up on first boot.
            def _set_ci():
                client.api.instances[safe_segment(name)].patch(
                    json={"config": {"cloud-init.user-data": cloud_init}},
                    params={"project": project_name},
                )
            await asyncio.to_thread(_set_ci)

        endpoint = client.api._api_endpoint  # e.g. https://host:8443/1.0
        session = client.api.session
        project_q = f"?project={project_name}" if project_name and project_name != "default" else ""

        def _do_rebuild() -> str:
            resp = session.post(
                f"{endpoint}/instances/{name}/rebuild{project_q}",
                json={"source": source},
                verify=False,
                timeout=(5, 120),
            )
            if resp.status_code >= 400:
                raise LXDOperationError(f"LXD rebuild failed: {resp.status_code} {resp.text[:300]}")
            op = resp.json()
            op_path = op.get("operation")  # /1.0/operations/<id>
            if not op_path:
                return ""  # completed synchronously
            op_id = op_path.rstrip("/").rsplit("/", 1)[-1]
            # (connect, read): a scalar would grant the same 1800s budget to the connect leg,
            # so a peer that never completes the handshake would pin this thread for half an
            # hour. The read leg genuinely needs to be long -- a rebuild is slow.
            wresp = session.get(f"{endpoint}{op_path[4:]}/wait", verify=False,
                                timeout=(_REBUILD_CONNECT_TIMEOUT, wait_timeout))
            wbody = wresp.json() or {}
            if wbody.get("type") == "error" or wbody.get("error"):
                raise LXDOperationError(f"LXD rebuild failed: {wbody.get('error') or 'unknown error'}")
            wdata = wbody.get("metadata") or {}
            if wdata.get("status") != "Success":
                raise LXDOperationError(
                    f"LXD rebuild operation failed: {wdata.get('err') or wdata.get('status') or 'unknown'}"
                )
            return op_id

        try:
            op_id = await run_blocking(
                _do_rebuild,
                # The socket timeouts above are what actually free the worker; this only
                # bounds how long the caller waits. Margin over wait_timeout so a normal
                # slow rebuild reports LXD's own result rather than this cap.
                timeout=wait_timeout + _GRACEFUL_READ_MARGIN_SECONDS,
                label=f"rebuild[{host}/{name}]",
                executor=_rebuild_executor,
                max_in_flight=_REBUILD_MAX_IN_FLIGHT,
            )
        except BlockingTimeout as exc:
            raise LXDOperationError(
                f"Rebuild of {name} on {host} did not complete within {wait_timeout}s"
            ) from exc
        except BlockingPoolFull as exc:
            raise LXDOperationError(
                f"Too many rebuilds in flight; retry {name} on {host} shortly"
            ) from exc
        logger.info("Rebuilt instance %s on %s (project=%s, op=%s)", name, host, project_name, op_id)
        return op_id

    @audit_operation(
        "lxd.instance.create", "instance",
        extract_details=lambda args, kwargs, result: {
            "image": kwargs.get("image"),
            "instance_type": kwargs.get("instance_type", "virtual-machine"),
            "profiles": kwargs.get("profiles"),
            "project": kwargs.get("project", "default"),
        }
    )
    async def create_vm(
        self,
        name: str,
        host: str,
        image: str,
        instance_type: str = "virtual-machine",
        profiles: Optional[List[str]] = None,
        config: Optional[Dict[str, str]] = None,
        cloud_init: Optional[str] = None,
        devices: Optional[Dict[str, Dict[str, str]]] = None,
        start_on_create: bool = True,
        project: str = "default",
    ) -> VMInfo:
        """Create a VM without running pylxd's blocking workflow on the event loop."""
        try:
            return await run_blocking(
                self._create_vm_blocking,
                name,
                host,
                image,
                instance_type,
                profiles,
                config,
                cloud_init,
                devices,
                start_on_create,
                project,
                timeout=_CREATE_WALL_TIMEOUT_SECONDS,
                label=f"create-vm[{host}/{project}/{name}]",
                executor=_create_executor,
                max_in_flight=_CREATE_MAX_IN_FLIGHT,
            )
        except BlockingTimeout as exc:
            raise LXDOperationError(
                f"VM creation on {host} timed out; it may still complete, so check the "
                "instance list before retrying"
            ) from exc
        except BlockingPoolFull as exc:
            raise LXDOperationError(
                "Too many VM creations are already in progress on this backend; retry shortly"
            ) from exc

    def _create_vm_blocking(
        self,
        name: str,
        host: str,
        image: str,
        instance_type: str,
        profiles: Optional[List[str]],
        config: Optional[Dict[str, str]],
        cloud_init: Optional[str],
        devices: Optional[Dict[str, Dict[str, str]]],
        start_on_create: bool,
        project: str,
    ) -> VMInfo:
        """Run the complete blocking create/start/readback transaction in one worker."""
        try:
            # Keep connection lookup and possible certificate reconnect in this worker too.
            pooled_client = self._get_client_with_reconnect(host)
            if pooled_client is None:
                raise LXDConnectionError(f"Could not connect to {host}")

            project = (project or "default").strip() or "default"
            # pylxd binds a project to the client. Creating through the pooled client would
            # silently place an authorized non-default request in `default`, then stamp the
            # audit trail with the requested project. Build the scoped client before issuing
            # any create call. The fallback preserves legacy default-project deployments whose
            # connection manager predates short-lived project clients.
            client = self.connection_manager.get_project_client(
                host,
                project,
                (5, _CREATE_LXD_READ_TIMEOUT_SECONDS),
            )
            if client is None:
                if project != "default":
                    raise LXDOperationError(
                        f"Could not open project '{project}' on {host}"
                    )
                client = pooled_client
            endpoint = self._get_node_endpoint(host)

            # Build instance configuration
            instance_config = config or {}

            # Add cloud-init if provided
            if cloud_init:
                instance_config["user.user-data"] = cloud_init

            # Determine source configuration
            source = {"type": "image"}

            # A full fingerprint or the prefix `lxc image list` prints; anything else an alias
            source[image_source_key(image)] = image

            # Create instance definition
            instance_def = {
                "name": name,
                "source": source,
                "type": instance_type,
                "profiles": profiles or ["default"],
                "config": instance_config,
            }
            
            if devices:
                instance_def["devices"] = devices

            # Create the instance
            if instance_type == "virtual-machine":
                instance = client.virtual_machines.create(
                    instance_def, 
                    wait=True
                )
            else:
                instance = client.containers.create(
                    instance_def, 
                    wait=True
                )

            logger.info(f"Created {instance_type} {name} on {host}")

            # Start if requested
            if start_on_create:
                instance = self._start_and_wait(instance, host, project)
                logger.info(f"Started {name} on {host}")

            vm_info = self._instance_to_vm_info(instance, host, endpoint)
            # Some pylxd versions omit `project` on a freshly-created wrapper. The project
            # selected above is authoritative and must match the response and audit record.
            vm_info.project = project
            return vm_info

        except LXDAPIException as e:
            raise LXDOperationError(f"Failed to create VM: {str(e)}")

    @audit_operation(
        "lxd.instance.create", "instance",
        extract_details=lambda args, kwargs, result: {
            "image": kwargs.get("image"),
            "instance_type": kwargs.get("instance_type", "virtual-machine"),
            "profiles": kwargs.get("profiles"),
        }
    )
    async def create_instance(
        self,
        name: str,
        host: str,
        image: Optional[str] = None,
        instance_type: str = "virtual-machine",
        profiles: Optional[List[str]] = None,
        config: Optional[Dict[str, str]] = None,
        cloud_init: Optional[str] = None,
        devices: Optional[Dict[str, Dict[str, str]]] = None,
        start_on_create: bool = True,
        project: str = "default",
        image_server: Optional[str] = None,
        image_protocol: Optional[str] = None,
        description: Optional[str] = None,
        source_type: str = "image",
    ) -> Optional[str]:
        """Start creating a new instance (non-blocking).

        The create is issued as a background LXD operation and this returns the
        operation id immediately. The (potentially slow) image pull happens
        server-side, so the HTTP request never waits on it. The instance appears
        via the polling instance list. If image_server is set, the image is
        pulled from that remote simplestreams server by alias; otherwise it is
        resolved locally (by fingerprint or alias). When start_on_create is set,
        a background task waits for the create to finish and then starts it.

        This backs the dashboard's `POST /instances` flow. create_vm() is kept
        separate and unchanged for Orcahub's `POST /vms`.
        """
        try:
            client = self._get_client_with_reconnect(host)
            if client is None:
                raise LXDConnectionError(f"Could not connect to {host}")

            project = project or "default"
            # Dedicated client with a long read timeout: the create POST itself
            # returns fast, but the background start step waits on operations that
            # can take a while when a remote image is being pulled.
            # pylxd's constructor does blocking network I/O (validates the
            # endpoint), so build it off the event loop like the create POST.
            create_client = await asyncio.to_thread(
                self.connection_manager.get_project_client,
                host, project, (5, 1800),
            )
            if create_client is None:
                if project != "default":
                    raise LXDOperationError(f"Could not open project '{project}' on {host}")
                create_client = client  # fall back to pooled (default project)

            # Fail fast on a duplicate name. LXD accepts a duplicate-name create as
            # an async 202 and only fails the background operation, which the
            # non-blocking flow can't surface cleanly (the pre-existing instance is
            # already in the list, so the failing placeholder looks "resolved").
            # A synchronous check gives the user an immediate, clear error.
            already_exists = await asyncio.to_thread(
                create_client.instances.exists, name
            )
            if already_exists:
                raise LXDOperationError(
                    f"An instance named '{name}' already exists in project '{project}' on {host}"
                )

            # important: fail fast on a NIC the project cannot reach. LXD accepts a create
            # like that as an async 202 and only fails the background operation, and the
            # console's placeholder row has nothing to clear it, so the instance sits at
            # "Creating..." with empty columns and no reason.
            #
            # The pooled client, not create_client: pylxd overwrites an explicit project
            # parameter with the client's own, so a project-bound client would check the
            # wrong project.
            try:
                await asyncio.to_thread(
                    check_nics, client, project, devices, profiles
                )
            except NicPreflightError as nic_err:
                raise LXDOperationError(str(nic_err)) from None


            # Build instance configuration
            instance_config = config or {}
            if cloud_init:
                instance_config["user.user-data"] = cloud_init

            # Determine source configuration
            if source_type == "none":
                # Blank VM: an empty root disk and no OS, equivalent to
                # `lxc init <name> --empty --vm`. There is nothing to boot yet --
                # an OS is installed afterwards by attaching an install ISO as
                # boot media and driving the VGA console.
                source = {"type": "none"}
            else:
                source = {"type": "image"}
                if image_server:
                    # Pull from a remote simplestreams server by alias.
                    source["mode"] = "pull"
                    source["server"] = image_server
                    source["protocol"] = image_protocol or "simplestreams"
                    source["alias"] = image
                else:  # local: a fingerprint (or its prefix) or an alias
                    source[image_source_key(image)] = image

            instance_def = {
                "name": name,
                "source": source,
                "type": instance_type,
                "profiles": profiles or ["default"],
                "config": _drop_unsupported_config(
                    create_client, instance_type, instance_config
                ),
            }
            if description:
                instance_def["description"] = description
            if devices:
                instance_def["devices"] = devices

            # Non-blocking create: POST kicks off a background LXD operation and
            # returns immediately. Synchronous validation errors (bad name, bad
            # pool, unknown image, etc.) still surface here as LXDAPIException.
            response = await asyncio.to_thread(
                lambda: create_client.api.instances.post(json=instance_def)
            )
            operation_id = None
            try:
                op_url = response.json().get("operation")
                if op_url:
                    operation_id = op_url.rstrip("/").rsplit("/", 1)[-1]
            except Exception:
                pass

            logger.info(
                f"Started creation of {instance_type} {name} on {host} "
                f"(project={project}, operation={operation_id})"
            )

            # "Create & Start": wait for the create operation in the background,
            # then start the instance, without blocking this request.
            if start_on_create:
                task = asyncio.create_task(
                    self._finalize_and_start(create_client, name, operation_id, host, project)
                )
                # Keep a strong ref until done so the loop can't GC it mid-run.
                self._bg_tasks.add(task)
                task.add_done_callback(self._bg_tasks.discard)

            else:
                # A create without auto-start had nobody watching its operation, so an
                # asynchronous failure was silent. Watch it too, holding the same strong
                # reference the branch above does so the loop cannot collect it mid-run.
                watch = asyncio.create_task(
                    self._watch_create_operation(create_client, name, operation_id, host, project)
                )
                self._bg_tasks.add(watch)
                watch.add_done_callback(self._bg_tasks.discard)

            return operation_id

        except ClientConnectionFailed as e:
            # Cluster unreachable during exists()/POST. Surface as a connection
            # error so the router maps it to 503, not a blanket 500.
            raise LXDConnectionError(f"Failed to reach {host}: {e}")
        except LXDAPIException as e:
            # A project limit refusal is the one synchronous 4xx the form can act on: it names
            # a field. Everything else stays the free text it always was.
            hit = project_limit_from_lxd_error(str(e))
            if hit is not None:
                raise LXDProjectLimitError(f"Failed to create VM: {str(e)}", hit)
            raise LXDOperationError(f"Failed to create VM: {str(e)}")

    @staticmethod
    def _invalidate_lists_after_background_change(name: str, host: str) -> None:
        """Clear the list caches a background job has just made stale. Never raises."""
        try:
            from app.core.cache import get_cache_manager
            get_cache_manager().invalidate_instance_cache()
        except Exception as e:  # noqa: BLE001 - a cache miss is cheaper than a lost start
            logger.warning(f"Could not clear the instance list cache after {name} on {host} changed: {e}")

    async def _judge_create_operation(self, client, name: str, operation_id: Optional[str],
                                      host: str, project: str) -> bool:
        """Wait for the queued create and write the terminal audit record. True if it landed.

        important: this is the single place a create is judged, and BOTH background branches go
        through it. They used to disagree. `_watch_create_operation` recorded a rejected create
        correctly but only ran when start_on_create was false, while the branch the form
        actually defaults to logged the same failure as `instance.autostart` and dropped the
        operation_id. So the guard written for this defect did not cover the path that hits it.

        The router records the create as `accepted` when LXD takes the request; this writes the
        verdict. One create, one final answer, either way.
        """
        if not operation_id:
            # Nothing queued to wait on. The synchronous path already answered.
            _announce_instance_created(host, project, name)
            return True

        from app.services.activity_logger import get_activity_logger

        try:
            await asyncio.to_thread(client.operations.wait_for_operation, operation_id)
        except Exception as e:
            logger.error(
                f"Create operation failed for {name} on {host} (project={project}): {e}"
            )
            try:
                get_activity_logger().log_activity(
                    action="instance.create",
                    target_type="instance",
                    target_id=name,
                    target_host=host,
                    result="failure",
                    error_code="LXD_OPERATION_FAILED",
                    error_message=str(e),
                    details={"project": project, "operation_id": operation_id},
                )
            except Exception:  # noqa: BLE001 - never let bookkeeping break the sweep
                pass
            return False

        logger.info(f"Create operation completed for {name} on {host} (project={project})")
        try:
            get_activity_logger().log_activity(
                action="instance.create",
                target_type="instance",
                target_id=name,
                target_host=host,
                result="success",
                details={"project": project, "operation_id": operation_id},
            )
        except Exception:  # noqa: BLE001
            pass

        # important: the add-on event is emitted HERE and not at the router, for the same reason
        # the terminal audit record is. The router answers "accepted" while LXD still has the
        # create queued, so announcing it there would tell every subscriber an instance exists
        # moments before LXD refuses it, with no correction to follow. This is the single place a
        # create is judged, so it is the only honest place to say it happened.
        from app.services.webhooks.emitter import emit_resource_event

        await emit_resource_event(
            event_type="orcastra.instance.created.v1",
            cluster_id=host,
            resource_type="instance",
            resource_name=name,
            project=project,
            operation_id=operation_id,
        )
        _announce_instance_created(host, project, name)
        return True

    async def _watch_create_operation(self, client, name: str, operation_id: Optional[str],
                                      host: str, project: str) -> None:
        """Background: record whether the create operation LXD queued actually succeeded.

        The no-autostart branch. Judging is shared with the autostart branch so the two cannot
        drift again; see _judge_create_operation.
        """
        await self._judge_create_operation(client, name, operation_id, host, project)

    async def _finalize_and_start(self, client, name: str, operation_id: Optional[str],
                                  host: str, project: str) -> None:
        """Background: wait for the create operation to finish, then start the instance.

        important: the create is judged BEFORE the start is attempted, and the two failures are
        recorded under different actions. This used to be one try block covering both, logging
        everything as `instance.autostart`, which meant a create LXD rejected was filed as a
        start problem on an instance that had never existed, without its operation_id. And
        because the form defaults start_on_create to true, that was the path almost every
        operator took: `_watch_create_operation`, written specifically to catch a rejected
        create, only ran on the other branch.
        """
        # Phase 1: did the instance actually get created?
        if not await self._judge_create_operation(client, name, operation_id, host, project):
            # The instance does not exist, so there is nothing to start and no second failure
            # to report. The create failure has already been recorded.
            return

        # Phase 2: it exists. A failure from here is genuinely about starting it.
        try:
            instance = await asyncio.to_thread(client.instances.get, name)
            await asyncio.to_thread(self._start_and_wait, instance, host, project)
            logger.info(f"Auto-started {name} on {host} (project={project})")
            # important: the list cache was cleared when the create was accepted, and the
            # console re-read it while the instance was still Stopped. Without clearing it
            # again here the row read Stopped for a full TTL after the cluster had it Running.
            self._invalidate_lists_after_background_change(name, host)
        except Exception as e:
            logger.error(
                f"Auto-start failed for {name} on {host} (project={project}): {e}"
            )
            # The instance exists but did not start. Distinct from a create failure: the
            # operator has something to look at and retry.
            try:
                from app.services.activity_logger import get_activity_logger
                get_activity_logger().log_activity(
                    action="instance.autostart",
                    target_type="instance",
                    target_id=name,
                    target_host=host,
                    result="failure",
                    error_message=str(e),
                    details={"project": project, "operation_id": operation_id},
                )
            except Exception:
                pass
