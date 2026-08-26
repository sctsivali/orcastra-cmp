"""
Instance/VM operations module.
Handles create, start, stop, restart, delete, and list operations for instances.
"""

import logging
import asyncio
from typing import List, Tuple, Optional, Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
from pylxd.exceptions import LXDAPIException, ClientConnectionFailed

from app.models.schemas import VMInfo
from app.core.config import get_settings
from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking
from .exceptions import (
    LXDConnectionError,
    LXDLocalStorageError,
    LXDOperationError,
    LXDGracefulTimeoutError,
    LXDSnapshotExistsError,
)
from .utils import (
    parse_vm_status,
    extract_ip_addresses,
    find_instance_in_projects,
    jsonable_lxd,
    normalize_cluster_member,
)
from .resources import ResourceManager
from .connection import LXDConnectionManager
from .audit import audit_operation

logger = logging.getLogger(__name__)

# The pooled client is built for fast failure detection (3s connect / 8s read).
# That read timeout is shorter than the graceful-shutdown waits callers ask for,
# so the HTTP read would expire before LXD's own operation timeout -- making the
# caller's `timeout` argument meaningless and surfacing a raw connection error
# instead of a clean result. Stop/restart therefore use a client whose read
# timeout outlives the requested wait by this margin.
_GRACEFUL_READ_MARGIN_SECONDS = 30

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
_rebuild_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="lxd_rebuild")
_REBUILD_MAX_IN_FLIGHT = 8


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
        resp = client.api.instances[name].snapshots.get(params={"project": project})
        meta = resp.json().get("metadata") or []
        return [str(u).rstrip("/").rsplit("/", 1)[-1] for u in meta]
    except Exception as e:  # noqa: BLE001 - absent/uncreatable snapshots -> treat as none
        logger.warning("Could not list snapshots for %s: %s", name, e)
        return []

# Thread pool for parallel instance state fetching
# Enterprise setting: 50 workers for high concurrency
_instance_executor = ThreadPoolExecutor(max_workers=50, thread_name_prefix="instance_state_")

# Failed-node skip is delegated to the shared circuit breaker
# (app.services.lxd.health) so every connection path skips the same dead cluster.


# note: LXD reports the literal string "none" for an instance on a non-clustered target, not an
# absent value. Passing that through means the UI prints "none" in a Cluster member field, which
# reads as a member called none rather than as "this target is not clustered". Normalised once
# here so neither consumer has to know.
# Kept as a local name: the helper is shared with the GPU inventory (see utils).
_cluster_member = normalize_cluster_member


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
    
    def _fetch_single_instance_state(self, client, instance_name: str, project_name: str) -> Tuple[str, List[str]]:
        """
        Fetch state for a single instance. Used by ThreadPoolExecutor.
        Returns tuple of (instance_name, ip_addresses).
        """
        ip_addrs = []
        try:
            state_resp = client.api.instances[instance_name].state.get(
                params={'project': project_name},
                timeout=(1, 3)  # Aggressive timeout for parallel calls
            )
            state_data = state_resp.json()['metadata']
            network_data = state_data.get('network', {})
            
            for iface_name, iface_data in network_data.items():
                addresses = iface_data.get('addresses', [])
                for addr in addresses:
                    family = addr.get('family')
                    address = addr.get('address')
                    scope = addr.get('scope', '')
                    if family == 'inet' or (family == 'inet6' and scope == 'global'):
                        if address:
                            ip_addrs.append(address)
        except Exception as e:
            logger.debug(f"Could not get state for {instance_name}: {e}")
        
        return instance_name, ip_addrs
    
    def _fetch_instance_states_parallel(
        self, 
        client, 
        running_instances: List[Tuple[str, str]], 
        node_name: str
    ) -> Dict[str, List[str]]:
        """
        Fetch states for multiple instances in parallel.
        
        Uses ThreadPoolExecutor to fetch all instance states concurrently.
        This reduces 83 sequential API calls (41 seconds) to parallel (few seconds).
        """
        if not running_instances:
            return {}
        
        instance_states = {}
        
        # Submit all tasks
        futures = {}
        for instance_name, project_name in running_instances:
            future = _instance_executor.submit(
                self._fetch_single_instance_state,
                client,
                instance_name,
                project_name
            )
            futures[future] = instance_name
        
        # Collect results with timeout
        for future in as_completed(futures, timeout=30):
            try:
                name, ips = future.result(timeout=5)
                instance_states[name] = ips
            except Exception as e:
                name = futures[future]
                logger.debug(f"Failed to get state for {name} on {node_name}: {e}")
                instance_states[name] = []
        
        logger.debug(f"Fetched state for {len(instance_states)}/{len(running_instances)} instances on {node_name}")
        return instance_states
    
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
    
    def _get_all_node_endpoints(self, node_names: list) -> dict:
        """Batch fetch all node endpoints to reduce Vault calls."""
        endpoints = {}
        try:
            settings = get_settings()
            if self.cert_manager and self.cert_manager.vault:
                for node_name in node_names:
                    try:
                        metadata = self.cert_manager.vault.vault_read(
                            f"{settings.vault_clusters_path}/{node_name}/metadata",
                            use_cache=True
                        )
                        endpoints[node_name] = metadata.get("endpoint", "unknown")
                    except Exception as e:
                        logger.debug(f"Could not get endpoint for {node_name}: {e}")
                        endpoints[node_name] = "unknown"
        except Exception as e:
            logger.warning(f"Failed to batch fetch endpoints: {e}")
        return endpoints
    
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
            profiles=list(instance.profiles) if instance.profiles else [],
            tags=tags,
            location=_cluster_member(getattr(instance, "location", None)),
        )
    
    async def list_all_vms(self) -> Tuple[List[VMInfo], List[str]]:
        """
        List all VMs across all configured LXD nodes and all projects.
        Returns tuple of (vm_list, connected_nodes).
        Uses PARALLEL fetching across nodes for better performance.
        """
        # Get nodes from Vault (source of truth)
        node_names = []
        if self.cert_manager:
            try:
                node_names = self.cert_manager.list_clusters()
                logger.debug(f"Found {len(node_names)} clusters in Vault: {node_names}")
            except Exception as e:
                logger.warning(f"Could not list Vault clusters: {e}")
                node_names = self.connection_manager.get_node_names()
        else:
            node_names = self.connection_manager.get_node_names()

        from app.core.org_scope import apply_cluster_filter
        node_names = apply_cluster_filter(node_names)
        
        if not node_names:
            logger.warning("No nodes found in Vault or connection manager")
            return [], []
        
        # Batch fetch all endpoints upfront to reduce Vault calls
        node_endpoints = self._get_all_node_endpoints(node_names)
        
        # Fetch from all nodes in PARALLEL using thread pool
        loop = asyncio.get_event_loop()
        tasks = [
            loop.run_in_executor(
                _instance_executor,
                self._fetch_vms_from_node,
                node_name,
                node_endpoints.get(node_name, "unknown")
            )
            for node_name in node_names
        ]
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Aggregate results
        all_vms: List[VMInfo] = []
        connected_nodes: List[str] = []
        
        for i, result in enumerate(results):
            node_name = node_names[i]
            if isinstance(result, Exception):
                logger.warning(f"Error fetching VMs from {node_name}: {result}")
                continue
            
            vms, connected = result
            if connected:
                connected_nodes.append(node_name)
                all_vms.extend(vms)
        
        logger.info(f"Fetched {len(all_vms)} VMs from {len(connected_nodes)} nodes")
        return all_vms, connected_nodes
    
    def _fetch_vms_from_node(
        self, 
        node_name: str, 
        node_endpoint: str
    ) -> Tuple[List[VMInfo], bool]:
        """
        Fetch all VMs from a single node (synchronous, for thread pool execution).
        Returns tuple of (vm_list, is_connected).
        """
        from app.services.lxd import health
        vms: List[VMInfo] = []

        # Skip clusters the shared breaker marked as recently failed.
        if health.is_tripped(node_name):
            logger.debug(f"Skipping {node_name} - recently failed (cached)")
            return [], False

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
                    return [], False
            
            if client is None:
                if health.get_kind(node_name) is None:
                    health.record_failure(node_name, "Client not connected")
                return [], False
            
            # Get all projects on this node
            try:
                projects = client.projects.all()
                project_names = [p.name for p in projects]
            except Exception as e:
                logger.warning(f"Could not list projects on {node_name}: {e}")
                project_names = ["default"]
            
            # Iterate through all projects
            for project_name in project_names:
                try:
                    response = client.api.instances.get(params={'project': project_name, 'recursion': 1})
                    instances_data = response.json()['metadata']
                    
                    # Collect running instances for parallel state fetch
                    running_instances = []
                    for inst in instances_data:
                        if inst.get('status') == 'Running':
                            running_instances.append((inst.get('name', ''), project_name))
                    
                    # Parallel fetch states for running instances
                    instance_states = {}
                    if running_instances:
                        instance_states = self._fetch_instance_states_parallel(
                            client, running_instances, node_name
                        )
                    
                    # Process all instances
                    for instance_data in instances_data:
                        try:
                            config_data = instance_data.get('config', {})
                            tags_str = config_data.get('user.tags', '')
                            tags = [t.strip() for t in tags_str.split(',') if t.strip()] if tags_str else []
                            
                            instance_name = instance_data.get('name', '')
                            ip_addrs = instance_states.get(instance_name, [])
                            
                            vm_info = VMInfo(
                                name=instance_name,
                                status=parse_vm_status(
                                    instance_data.get('status', 'Unknown'),
                                    instance_data.get('status_code', 0)
                                ),
                                status_code=instance_data.get('status_code', 0),
                                ip_addresses=ip_addrs,
                                host=node_name,
                                host_endpoint=node_endpoint,
                                architecture=instance_data.get('architecture', ''),
                                created_at=instance_data.get('created_at'),
                                last_used_at=instance_data.get('last_used_at'),
                                description=instance_data.get('description', ''),
                                type=instance_data.get('type', 'container'),
                                resources=None,
                                config=config_data,
                                devices=instance_data.get('devices') or {},
                                expanded_devices=instance_data.get('expanded_devices') or {},
                                profiles=instance_data.get('profiles', []),
                                tags=tags,
                                project=project_name,
                                location=_cluster_member(instance_data.get('location')),
                            )
                            
                            if vm_info.config is None:
                                vm_info.config = {}
                            vm_info.config['project'] = project_name
                            
                            vms.append(vm_info)
                        except Exception as e:
                            logger.error(f"Error processing instance on {node_name}: {e}")
                            
                except Exception as e:
                    logger.error(f"Error listing instances in project '{project_name}' on {node_name}: {e}")
            
            return vms, True
            
        except Exception as e:
            logger.warning(f"Error fetching VMs from {node_name}: {e}")
            return [], False
    
    async def get_vm(self, name: str, host: str) -> Optional[VMInfo]:
        """Get a specific VM by name and host (searches all projects)."""
        try:
            client = self._get_client_with_reconnect(host)
            if client is None:
                raise LXDConnectionError(f"Could not connect to {host}")
            endpoint = self._get_node_endpoint(host)
            
            instance, project_name = find_instance_in_projects(client, name)
            
            if instance is None:
                return None
            
            vm_info = self._instance_to_vm_info(instance, host, endpoint)
            if vm_info.config is None:
                vm_info.config = {}
            vm_info.config['project'] = project_name
            
            return vm_info
            
        except LXDConnectionError:
            raise
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
                instance.start(wait=True)
                instance.sync()
                logger.info(f"Started VM {name} on {host} (project: {project_name})")
                vm_info = self._instance_to_vm_info(instance, host, endpoint)
            
            if vm_info.config is None:
                vm_info.config = {}
            vm_info.config['project'] = project_name
            
            return vm_info
            
        except LXDAPIException as e:
            raise LXDOperationError(f"Failed to start VM: {str(e)}")
    
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
                return waiting_client.instances.get(instance.name)
        except Exception as e:  # noqa: BLE001 - fall back to the pooled client
            logger.warning(
                "Could not build a long-timeout client for %s on %s: %s", instance.name, host, e
            )
        return instance

    @staticmethod
    def _graceful_timeout(name: str, action: str, timeout: int) -> LXDGracefulTimeoutError:
        # A graceful stop only works if software inside the guest acts on the ACPI
        # power event. A fully-booted OS does so in about a second; the cases below
        # never will, no matter how long the timeout.
        return LXDGracefulTimeoutError(
            f"Timed out after {timeout}s waiting for '{name}' to shut down gracefully: "
            f"nothing in the guest acted on the ACPI shutdown request. Common causes: "
            f"no OS is installed, it is sitting at a boot menu or UEFI shell, it is "
            f"still early in boot, or the OS does not handle ACPI power events (many "
            f"live/installer images do not). Retry the {action} with force to "
            f"power-cycle it."
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

            if vm_info.config is None:
                vm_info.config = {}
            vm_info.config['project'] = project_name

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

            if vm_info.config is None:
                vm_info.config = {}
            vm_info.config['project'] = project_name

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
    ) -> bool:
        """Delete a VM (searches all projects)."""
        try:
            client = self._get_client_with_reconnect(host)
            if client is None:
                raise LXDConnectionError(f"Could not connect to {host}")
            
            instance, project_name = find_instance_in_projects(client, name, project)
            
            if instance is None:
                raise LXDOperationError(f"VM {name} not found on {host}")
            
            # Stop an active instance before deleting when force is set. A Frozen
            # instance (billing-paused) is still active, so LXD refuses to delete
            # it until it is stopped -- force-stop covers both Running and Frozen.
            if instance.status in ("Running", "Frozen"):
                if force:
                    instance.stop(force=True, wait=True)
                else:
                    raise LXDOperationError(
                        f"VM {name} is {instance.status.lower()}. Stop it first or use force=true"
                    )

            instance.delete(wait=True)
            
            logger.info(f"Deleted VM {name} from {host} (project: {project_name})")
            return True
            
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
                client.api.instances[name].patch(
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
    ) -> VMInfo:
        """Create a new VM."""
        try:
            client = self._get_client_with_reconnect(host)
            if client is None:
                raise LXDConnectionError(f"Could not connect to {host}")
            endpoint = self._get_node_endpoint(host)
            
            # Build instance configuration
            instance_config = config or {}
            
            # Add cloud-init if provided
            if cloud_init:
                instance_config["user.user-data"] = cloud_init
            
            # Determine source configuration
            source = {"type": "image"}
            
            # Check if image is a fingerprint or alias
            if len(image) == 64:  # Fingerprint
                source["fingerprint"] = image
            else:
                source["alias"] = image
            
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
                instance.start(wait=True)
                instance.sync()
                logger.info(f"Started {name} on {host}")
            
            return self._instance_to_vm_info(instance, host, endpoint)
            
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
        operation id immediately — the (potentially slow) image pull happens
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
                elif len(image) == 64:  # Local fingerprint
                    source["fingerprint"] = image
                else:  # Local alias
                    source["alias"] = image

            instance_def = {
                "name": name,
                "source": source,
                "type": instance_type,
                "profiles": profiles or ["default"],
                "config": instance_config,
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
            # then start the instance — without blocking this request.
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
            # Cluster unreachable during exists()/POST — surface as a connection
            # error so the router maps it to 503, not a blanket 500.
            raise LXDConnectionError(f"Failed to reach {host}: {e}")
        except LXDAPIException as e:
            raise LXDOperationError(f"Failed to create VM: {str(e)}")

    async def _watch_create_operation(self, client, name: str, operation_id: Optional[str],
                                      host: str, project: str) -> None:
        """Background: record whether the create operation LXD queued actually succeeded.

        important: the POST returns as soon as LXD accepts the request, and only synchronous
        validation errors reach the caller. Anything that fails while the operation RUNS, an
        image that is not on the host being the common one, left the API reporting success with
        an operation id and the audit trail recording result=success for an instance that never
        existed. The events stream shows it to whoever has the page open; this makes it visible
        to everyone else, including the audit trail.
        """
        if not operation_id:
            return
        try:
            await asyncio.to_thread(client.operations.wait_for_operation, operation_id)
        except Exception as e:
            logger.error(
                f"Create operation failed for {name} on {host} (project={project}): {e}"
            )
            try:
                from app.services.activity_logger import get_activity_logger
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
            return
        logger.info(f"Create operation completed for {name} on {host} (project={project})")

    async def _finalize_and_start(self, client, name: str, operation_id: Optional[str],
                                  host: str, project: str) -> None:
        """Background: wait for the create operation to finish, then start the
        instance (the 'Create & Start' flow). Runs off the request path; failures
        are logged rather than surfaced to the caller."""
        try:
            if operation_id:
                await asyncio.to_thread(client.operations.wait_for_operation, operation_id)
            instance = await asyncio.to_thread(client.instances.get, name)
            await asyncio.to_thread(lambda: instance.start(wait=True))
            logger.info(f"Auto-started {name} on {host} (project={project})")
        except Exception as e:
            logger.error(
                f"Auto-start failed for {name} on {host} (project={project}): {e}"
            )
            # The create already logged success; without this, a failed auto-start
            # is invisible to the dashboard. Record it on the activity trail.
            try:
                from app.services.activity_logger import get_activity_logger
                get_activity_logger().log_activity(
                    action="instance.autostart",
                    target_type="instance",
                    target_id=name,
                    target_host=host,
                    result="failure",
                    error_message=str(e),
                    details={"project": project},
                )
            except Exception:
                pass
