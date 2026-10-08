"""
LXD Service - Main integration module.
Provides unified interface for all LXD operations.
"""

import asyncio
import logging
import time
from typing import Callable, List, Tuple, Optional, Dict, Any
from concurrent.futures import ThreadPoolExecutor

from app.models.schemas import (
    VMInfo,
    ImageInfo,
    NodeResources,
    NodeStatus,
    InstanceMetrics,
)
from app.core.blocking import run_blocking
from .availability import require_client
from .connection import LXDConnectionManager
from .gpu import GpuInventory, build_gpu_inventory
from .lazy_client import connect_or_get_client
from .node_resources import resources_for_host
from .project_usage import ProjectUsage, read_project_usage
from .freshness import read_resources
from .resources import ResourceManager
from .instance_metrics import read_instance_metrics
from .instance import FleetInventory, InstanceManager
from .projects import ProjectManager
from .images import ImageManager, ImageStoreListing
from app.core.type_utils import normalize_node_type, resolve_clustered
from .exceptions import LXDConnectionError, LXDLocalStorageError
from app.services.lxd_cert_manager import LXDCertificateManager
from app.core.vault_client import VaultClient
from app.core.config import get_settings
from app.services.lxd.project_scope import list_instance_records

logger = logging.getLogger(__name__)

# Shared thread pool for blocking operations
_blocking_executor = ThreadPoolExecutor(max_workers=10, thread_name_prefix="lxd_blocking")

# important: instance metrics get their own pool. The node-status sweep submits to
# _blocking_executor through a bare run_in_executor that run_blocking cannot count, so a sweep
# could take every thread and leave a five-second poll queueing until its budget ran out, which
# the dialog then reported as "Cluster did not answer" for a healthy instance. The per-cluster
# cap keeps one slow cluster from holding the whole pool.
_instance_metrics_executor = ThreadPoolExecutor(max_workers=10, thread_name_prefix="instance_metrics")
_INSTANCE_METRICS_TIMEOUT_SECONDS = 12.0
_INSTANCE_METRICS_MAX_IN_FLIGHT = 10
_INSTANCE_METRICS_MAX_IN_FLIGHT_PER_CLUSTER = 3

# Failed-node skip is delegated to the shared circuit breaker
# (app.services.lxd.health) so every connection path - status checks, instance
# and project fan-outs, warning sweeps - skips the same dead cluster.


class LXDService:
    """
    Main service class for managing LXD operations across multiple nodes.
    Provides unified interface for VM management, projects, images, and resources.
    """
    
    def __init__(self):
        # Initialize managers
        self.connection_manager = LXDConnectionManager()
        self.resource_manager = ResourceManager()
        settings = get_settings()
        vault_client = VaultClient(settings.vault_addr, settings.vault_token)
        self.cert_manager = LXDCertificateManager(vault_client)
        self.instance_manager = InstanceManager(self.connection_manager, self.resource_manager, self.cert_manager)
        self.project_manager = ProjectManager(self.connection_manager, self.cert_manager)
        self.image_manager = ImageManager(self.connection_manager, self.cert_manager)
        
        # Sync Vault-registered clusters into connection_manager whitelist
        # so get_node_names() only returns registered nodes
        try:
            registered = self.cert_manager.list_clusters()
            self.connection_manager.sync_registered_clusters(registered)
        except Exception as e:
            logger.warning(f"Could not sync registered clusters at init: {e}")
    
    def ensure_connection(
        self,
        cluster_id: str,
        *,
        expected_server_fingerprint: str | None = None,
    ) -> bool:
        """
        Ensure cluster is connected (lazy loading helper).
        Respects org-scope: returns False if the cluster is outside user's org.
        """
        # Org-scope guard: prevent reconnecting to clusters outside the user's org
        from app.core.org_scope import get_current_allowed_clusters
        from app.services.lxd import health
        from app.services.lxd.exceptions import (
            LXDAuthorizationError,
            LXDTransportPinError,
        )
        allowed = get_current_allowed_clusters()
        if allowed is not None and cluster_id not in allowed:
            return False

        # Check if already connected
        client = (
            self.connection_manager.get_client(cluster_id)
            if expected_server_fingerprint is None
            else self.connection_manager.get_client(
                cluster_id,
                expected_server_fingerprint=expected_server_fingerprint,
            )
        )
        if client:
            return True

        # important: fast-skip clusters the breaker marked as recently failed -
        # this is the project/instance lazy-connect path; without it every
        # fan-out re-pays the dead-cluster connect timeout (the projects stall).
        if health.is_tripped(cluster_id):
            return False

        # Try to connect
        try:
            # important: the metadata path, not get_cluster_info. That helper also reads
            # <cluster>/secrets to compute a has_trust_password flag nothing on this path
            # looks at, and only /metadata is on the Vault read cache allowlist
            # (core/vault_client.py CACHEABLE_PATH_SUFFIXES), so the flag cost one uncached
            # round trip on every cold connect. Certificates are deliberately never cached,
            # so this is the only Vault read here that could be removed at all.
            metadata = self.cert_manager.vault.vault_read(
                f"{get_settings().vault_clusters_path}/{cluster_id}/metadata"
            )
            cert_data = self.cert_manager.get_cluster_certificate(cluster_id)
            
            connect_kwargs = {
                "cluster_id": cluster_id,
                "endpoint": metadata["endpoint"],
                "cert_content": cert_data["client.crt"],
                "key_content": cert_data["client.key"],
                "verify": False,
            }
            if expected_server_fingerprint is not None:
                connect_kwargs["expected_server_fingerprint"] = (
                    expected_server_fingerprint
                )
            connected = self.connection_manager.connect_with_vault_cert(
                **connect_kwargs
            )
            # note: this returns None when the breaker refuses it. Treating that as success
            # cleared the breaker and reported a cluster as connected that never was.
            if connected is None:
                return False
            logger.info(f"Lazy-connected to cluster: {cluster_id}")
            health.clear(cluster_id)
            return True
        except LXDTransportPinError:
            raise
        except LXDAuthorizationError as e:
            # important: the host ANSWERED and refused our certificate. Letting this fall into
            # the broad handler below recorded it as a generic failure, so the breaker's kind
            # reverted to "offline" one line after connect_with_vault_cert had correctly
            # recorded "unauthorized". Five clusters on the dev fleet answered HTTP 200 and
            # still read Offline on every surface because of these two lines.
            logger.warning(f"Cluster {cluster_id} refused our certificate: {e}")
            health.record_failure(cluster_id, str(e), kind=health.KIND_UNAUTHORIZED)
            return False
        except LXDLocalStorageError:
            # important: narrow, and above the broad handler for the same reason the
            # LXDAuthorizationError branch is. The backend's own disk failed, so the peer was
            # never contacted and there is nothing true to record about it. Letting this reach
            # record_failure below marked every cluster offline at once while the hypervisors
            # were healthy, and the breaker then suppressed the retries that would have shown it.
            raise
        except Exception as e:
            # important: WARNING, not ERROR. See connection.py: an offline cluster is
            # expected operational noise, and the breaker below already records it.
            logger.warning(f"Failed to connect cluster {cluster_id}: {e}")
            health.record_failure(cluster_id, str(e))
            return False
    
    def get_client_with_reconnect(self, cluster_id: str):
        """
        Get LXD client with automatic reconnection if stale.
        This method properly adds reconnected clients back to the connection pool.
        """
        client = self.connection_manager.get_client(cluster_id)
        if client is None:
            logger.info(f"Connection to {cluster_id} is stale, attempting reconnect...")
            # Use ensure_connection which properly adds client to connection_manager._clients
            if self.ensure_connection(cluster_id):
                client = self.connection_manager.get_client(cluster_id)
                logger.info(f"Reconnected to {cluster_id} successfully")
            else:
                logger.warning(f"Reconnect failed for {cluster_id}")
                client = None
        return client
    
    # Node operations
    def get_node_names(self) -> List[str]:
        """Get list of all registered cluster names from Vault."""
        from app.core.org_scope import apply_cluster_filter
        return apply_cluster_filter(self.cert_manager.list_clusters())
    
    def check_nodes_status(self) -> List[NodeStatus]:
        """Check connection status for all nodes (sync version - deprecated, use async version)."""
        statuses = []
        settings = get_settings()
        node_names = self.get_node_names()

        for node_name in node_names:
            status = self._check_single_node_status(node_name, settings)
            statuses.append(status)
        
        return statuses
    
    def check_node_status(self, node_name: str) -> NodeStatus:
        """Probe one node and return its status, resources included.

        The public entry point for callers outside the fan-out, which need one node rather than
        the whole fleet.
        """
        return self._check_single_node_status(node_name, get_settings())

    def _read_registry(self, node_name: str, settings) -> Dict[str, Any]:
        """What registration recorded about a cluster, none of which depends on it answering.

        The endpoint was pulled onto every path first, for the reason in the caller. The display
        name and the type are the same kind of value and had the same defect: a cluster that did
        not answer came back with no name and no type, so every picker and every card printed its
        raw id and called it a bare hypervisor. Vault has held both the whole time.

        note: `type` is normalised only when one was recorded. normalize_node_type(None) answers
        "lxd", which would put back the everything-is-LXD default this removes.

        note: `clustered` rides along for free. This read already happens on every path, so the
        clusters table and the dashboard node card can stop disagreeing about whether an offline
        host is clustered without paying anything for it. An earlier note in
        documentation/clusters/hypervisor-type-and-clustering.md claimed the opposite, that
        giving /nodes the stored fallback would mean a storage read on a per-cluster fan-out.
        It would not; the record is already in hand and the key was simply dropped on the way
        out.
        """
        try:
            metadata = self.cert_manager.vault.vault_read(
                f"{settings.vault_clusters_path}/{node_name}/metadata"
            )
        except Exception:
            return {
                "endpoint": "unknown",
                "display_name": None,
                "node_type": None,
                "clustered": None,
            }
        registered_type = metadata.get("type")
        stored_clustered = metadata.get("clustered")
        return {
            "endpoint": metadata.get("endpoint", "unknown"),
            "display_name": metadata.get("display_name") or None,
            "node_type": normalize_node_type(registered_type) if registered_type else None,
            # A stored value is only ever written by check-auth, after a trusted answer, so an
            # absent key means unknown rather than standalone. Anything that is not a bool is
            # not a reading.
            "clustered": stored_clustered if isinstance(stored_clustered, bool) else None,
        }

    def _check_single_node_status(self, node_name: str, settings) -> NodeStatus:
        """Check status for a single node (internal helper)."""
        from app.services.lxd import health

        # important: read the registration BEFORE the breaker check. Endpoint, display name and
        # type are registry metadata and have nothing to do with reachability, but returning them
        # only on the healthy path meant every dead cluster reported endpoint="unknown", no name
        # and no type, so a picker printed the word "unknown" and the cluster's raw id. The
        # breaker exists to skip an 8s connect, not a local Vault read.
        registry = self._read_registry(node_name, settings)
        endpoint = registry["endpoint"]

        # Skip clusters the shared breaker marked as recently failed.
        if health.is_tripped(node_name):
            logger.debug(f"Skipping {node_name} - recently failed (cached)")
            # The breaker remembers WHY it tripped, so a cluster that answered and refused our
            # certificate keeps reading as untrusted instead of degrading to "offline" for the
            # 60s the breaker holds.
            return NodeStatus(
                name=node_name,
                endpoint=endpoint,
                display_name=registry["display_name"],
                node_type=registry["node_type"],
                clustered=registry["clustered"],
                connected=False,
                reachable=health.get_kind(node_name) == health.KIND_UNAUTHORIZED,
                error=health.get_reason(node_name) or "Node recently failed - retry later"
            )

        try:
            
            # Use get_client_with_reconnect for automatic reconnection
            client = self.get_client_with_reconnect(node_name)
            if client:
                status = self.resource_manager.check_node_status(
                    client, node_name, endpoint, stored_clustered=registry["clustered"]
                )
                return status
            else:
                # important: get_client has already recorded WHY it failed, and an untrusted
                # certificate records KIND_UNAUTHORIZED. Overwriting that with a generic
                # "Client not connected" downgrades a cluster that answered and refused us
                # into one that looks powered off, and the breaker then serves that wrong
                # answer to every reader for its whole TTL.
                # important: same rule as instance.py. A missing client is not evidence the
                # cluster failed, and recording one here trips the breaker for a cluster this
                # worker has merely never connected to. Report it offline for this response
                # and leave the door open. Only a path that actually reached the peer records.
                kind = health.get_kind(node_name) or health.KIND_OFFLINE
                return NodeStatus(
                    name=node_name,
                    endpoint=endpoint,
                    display_name=registry["display_name"],
                    node_type=registry["node_type"],
                    clustered=registry["clustered"],
                    connected=False,
                    reachable=kind == health.KIND_UNAUTHORIZED,
                    error=health.get_reason(node_name) or "Client not connected"
                )
        except LXDLocalStorageError:
            # See ensure_connection above. The node is not the thing that failed, so it must not
            # be the thing recorded, and a NodeStatus claiming "not connected" would be a guess.
            raise
        except Exception as e:
            logger.warning(f"Node {node_name} health check failed: {e}")
            kind = health.get_kind(node_name)
            if kind is None:
                health.record_failure(node_name, str(e))
                kind = health.KIND_OFFLINE
            return NodeStatus(
                name=node_name,
                endpoint=endpoint,
                display_name=registry["display_name"],
                node_type=registry["node_type"],
                connected=False,
                reachable=kind == health.KIND_UNAUTHORIZED,
                error=str(e)
            )
    
    async def check_nodes_status_async(self) -> List[NodeStatus]:
        """Check connection status for all nodes in parallel (async version)."""
        settings = get_settings()
        node_names = self.get_node_names()
        
        if not node_names:
            return []
        
        # Run all node checks in parallel using thread pool
        loop = asyncio.get_event_loop()
        tasks = [
            loop.run_in_executor(
                _blocking_executor,
                self._check_single_node_status,
                node_name,
                settings
            )
            for node_name in node_names
        ]
        
        statuses = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Filter out exceptions and log them
        result = []
        for i, status in enumerate(statuses):
            if isinstance(status, Exception):
                logger.warning(f"Node check failed for {node_names[i]}: {status}")
                result.append(NodeStatus(
                    name=node_names[i],
                    endpoint="unknown",
                    connected=False,
                    reachable=False,
                    error=str(status)
                ))
            else:
                result.append(status)
        
        return result
    
    def get_node_resources(self, node_name: str) -> Dict[str, Any]:
        """Get available resources for a specific node.

        important: require_client, like every sibling here. Without it a breaker-tripped
        cluster arrived as a None dereference and the route answered 500, sending an operator
        to look for a backend fault when the node was simply not answering.
        """
        client = require_client(self.connection_manager.get_client(node_name), node_name)
        data = self.resource_manager.get_node_resources(client, node_name)
        # A clustered host's body is one member's. The project quota pre-flight and the
        # server resources route read this dict, and a cap bounded by one member's threads
        # refused projects the cluster could hold. Same aggregate, same cache, as the card,
        # through the one place that decision is made (resources_for_host).
        if resolve_clustered(client=client):
            return resources_for_host(
                client, node_name, data, GpuInventory.empty(), True
            ).model_dump()
        return data

    def get_node_resources_detail(self, node_name: str) -> NodeResources:
        """Full NodeResources for one node, GPU included.

        important: GET /nodes/{node}/resources is typed as NodeResources but was built from
        the CPU/memory/storage dict alone, so every GPU field it declared was served as its
        default. The endpoint reported zero GPUs on hardware that has one.
        """
        client = require_client(connect_or_get_client(self, node_name), node_name)
        # important: one live read for both consumers. Reading twice doubles the round trips
        # and lets the memory figure and the GPU inventory disagree about the same host.
        live = read_resources(client, node_name)
        data = self.resource_manager.get_node_resources(client, node_name, resources=live)
        gpu = GpuInventory.empty()
        try:
            # recursion=1: build_gpu_inventory reads location, status and the expanded
            # config off each instance, which at recursion=0 is a request apiece.
            gpu = build_gpu_inventory(live.body, list_instance_records(client)[0])
        except Exception as e:
            logger.debug(f"Could not read GPU inventory for {node_name}: {e}")
        return resources_for_host(
            client, node_name, data, gpu, resolve_clustered(client=client)
        )

    def get_node_gpus(self, node_name: str) -> GpuInventory:
        """List the GPUs on a specific node, with their class and allocation state."""
        client = require_client(connect_or_get_client(self, node_name), node_name)
        return self.resource_manager.get_node_gpus(client, node_name)
    
    # Instance/VM operations
    async def list_all_vms(self) -> Tuple[List[VMInfo], List[str]]:
        """
        List all VMs across all configured LXD nodes and all projects.
        Returns tuple of (vm_list, connected_nodes).
        """
        return await self.instance_manager.list_all_vms()

    async def list_all_vms_with_failures(
        self,
    ) -> Tuple[List[VMInfo], List[str], List[str]]:
        """List instances and identify clusters whose project inventory was incomplete."""
        return await self.instance_manager.list_all_vms_with_failures()

    async def read_fleet_inventory(self) -> FleetInventory:
        """The monitoring sweep's fleet read: clusters out of time are named, not raised."""
        return await self.instance_manager.read_fleet_inventory()

    async def list_vms_in_project_with_failures(
        self,
        node_name: str,
        project: str,
    ) -> Tuple[List[VMInfo], List[str], List[str]]:
        """List instances in one exact cluster/project and preserve inventory failure detail."""
        return await self.instance_manager.list_vms_in_project_with_failures(
            node_name,
            project,
        )
    
    async def get_vm(
        self,
        name: str,
        host: str,
        project: Optional[str] = None,
        project_filter: Optional[Callable[[str], bool]] = None,
    ) -> Optional[VMInfo]:
        """Get a specific VM by name and host, narrowed to a project when one is known."""
        if project_filter is None:
            return await self.instance_manager.get_vm(name, host, project)
        return await self.instance_manager.get_vm(
            name, host, project, project_filter=project_filter
        )
    
    async def start_vm(
        self,
        name: str,
        host: str,
        force: bool = False,
        project: Optional[str] = None,
    ) -> VMInfo:
        """Start a VM (searches all projects)."""
        return await self.instance_manager.start_vm(name, host, force, project)
    
    async def stop_vm(
        self, 
        name: str, 
        host: str, 
        force: bool = False,
        timeout: int = 30,
        project: Optional[str] = None,
    ) -> VMInfo:
        """Stop a VM (searches all projects)."""
        return await self.instance_manager.stop_vm(name, host, force, timeout, project)
    
    async def restart_vm(
        self, 
        name: str, 
        host: str, 
        force: bool = False,
        timeout: int = 30,
        project: Optional[str] = None,
    ) -> VMInfo:
        """Restart a VM (searches all projects)."""
        return await self.instance_manager.restart_vm(name, host, force, timeout, project)
    
    async def delete_vm(
        self, 
        name: str, 
        host: str, 
        force: bool = False,
        project: Optional[str] = None,
    ) -> str:
        """Delete a VM, returning the project it was actually deleted from."""
        return await self.instance_manager.delete_vm(name, host, force, project)

    async def get_rebuild_preflight(self, name: str, host: str, project: Optional[str] = None):
        """Status + snapshots + config/attached-volumes snapshot for a rebuild."""
        return await self.instance_manager.get_rebuild_preflight(name, host, project)

    async def rebuild_instance(
        self,
        name: str,
        host: str,
        source: dict,
        project: Optional[str] = None,
        cloud_init: Optional[str] = None,
        wait_timeout: int = 1800,
    ) -> str:
        """Rebuild an instance's root disk from `source` (preserves identity/config/volumes)."""
        return await self.instance_manager.rebuild_instance(
            name, host, source, project, cloud_init, wait_timeout
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
        """Create a new VM (blocking). Stable contract for `POST /vms` (Orcahub)."""
        return await self.instance_manager.create_vm(
            name=name,
            host=host,
            image=image,
            instance_type=instance_type,
            profiles=profiles,
            config=config,
            cloud_init=cloud_init,
            devices=devices,
            start_on_create=start_on_create,
            project=project,
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
        job_id: Optional[str] = None,
    ) -> Optional[str]:
        """Start creating a new instance (non-blocking); returns the operation id.

        Backs the dashboard's `POST /instances`. See InstanceManager.create_instance.
        `source_type="none"` creates a blank VM (empty root disk, no OS).
        """
        return await self.instance_manager.create_instance(
            name, host, image, instance_type, profiles, config,
            cloud_init, devices, start_on_create, project,
            image_server, image_protocol, description, source_type, job_id,
        )

    async def fail_create_job(
        self,
        job_id: str,
        *,
        error: str,
        error_code: str,
    ) -> None:
        await self.instance_manager.fail_create_job(
            job_id,
            error=error,
            error_code=error_code,
        )

    async def recover_create_operation_jobs(self) -> int:
        return await self.instance_manager.recover_create_operation_jobs()

    # Image operations
    async def list_images(self, host: str, project: Optional[str] = None) -> ImageStoreListing:
        """The images `project` can use on a host (its own store or the shared default one),
        or every store on the host when no project is named."""
        return await self.image_manager.list_images(host, project)

    # note: the two below are deliberately sync. pylxd blocks, and a delete waits on a real
    # LXD operation, so the router hands them to run_blocking with an explicit timeout rather
    # than this layer hiding the block behind an async signature.
    def image_store_of(self, host: str, project: Optional[str] = None) -> str:
        """The store `project` reads on a host, which is what a caller must be allowed to see."""
        return self.image_manager.image_store_of(host, project)

    def image_stores_of(self, host: str, projects) -> Dict[str, str]:
        """The store each named project reads on a host, from one read of the project configs."""
        return self.image_manager.image_stores_of(host, projects)

    def get_image(
        self, host: str, fingerprint: str, project: Optional[str] = None
    ) -> Optional[ImageInfo]:
        """Read one image's metadata from the store `project` reads. None when it is not there."""
        return self.image_manager.get_image(host, fingerprint, project)

    def delete_image(self, host: str, fingerprint: str, project: Optional[str] = None) -> bool:
        """Delete an image from the store `project` reads. False when it was already gone."""
        return self.image_manager.delete_image(host, fingerprint, project)
    
    # Project operations
    async def list_projects(self, host: str) -> List[Dict[str, Any]]:
        """List all projects on a specific LXD host."""
        # ProjectManager owns the bounded direct collection read. Ensuring a connection here first
        # would add Vault and network I/O synchronously on the request event loop.
        return await self.project_manager.list_projects(host)

    async def get_project_usage(self, host: str, project: str) -> ProjectUsage:
        """How much of each of `project`'s limits is allocated (see services/lxd/project_usage)."""
        return await asyncio.to_thread(self._read_project_usage_sync, host, project)

    def _read_project_usage_sync(self, host: str, project: str) -> ProjectUsage:
        # note: the client lookup revalidates the pooled connection with a live request, so it
        # runs on the thread with the read, never inline on the loop.
        self.ensure_connection(host)
        client = require_client(self.connection_manager.get_client(host), host)
        return read_project_usage(client, project)
    
    async def create_project(
        self, 
        host: str, 
        name: str, 
        description: Optional[str] = None,
        config: Optional[Dict[str, str]] = None,
        max_cpu: Optional[int] = None,
        max_ram: Optional[str] = None,
        max_storage: Optional[str] = None
    ) -> Dict[str, Any]:
        """Create a new project on a specific LXD host."""
        return await self.project_manager.create_project(
            host, name, description, config, max_cpu, max_ram, max_storage
        )
    
    async def update_project(
        self, 
        host: str, 
        project_name: str, 
        description: Optional[str] = None,
        max_cpu: Optional[int] = None,
        max_ram: Optional[str] = None,
        max_storage: Optional[str] = None
    ) -> Dict[str, Any]:
        """Update an existing project's description and resource limits."""
        return await self.project_manager.update_project(
            host, project_name, description, max_cpu, max_ram, max_storage
        )
    
    async def delete_project(self, host: str, name: str) -> None:
        """Delete a project from a specific LXD host."""
        return await self.project_manager.delete_project(host, name)
    
    # Metrics operations
    def _get_instance_metrics_blocking(
        self, 
        host: str, 
        instance_name: str,
        project: str = "default"
    ) -> InstanceMetrics:
        """Read one metrics sample. Must run on the instance metrics executor."""
        # important: connect_or_get_client, not connection_manager.get_client. The latter is
        # memory-only and answers None in a worker that has never connected to this cluster,
        # so with four workers the same poll answered 200 or 503 depending on which one took it.
        client = connect_or_get_client(self, host)
        # important: without this, a None client reaches find_instance_in_projects, which
        # swallows the AttributeError in both of its loops and returns (None, None). The
        # caller then reports "Instance '<name>' not found on <host>" and the router
        # message-sniffs "not found" into a 404 -- telling the operator their instance was
        # deleted when the cluster is merely offline.
        if client is None:
            raise LXDConnectionError(f"Could not connect to {host}")
        # note: two seconds under the wall budget, so a slow /1.0/metrics read is dropped
        # before run_blocking gives up on the whole sample.
        deadline = time.monotonic() + _INSTANCE_METRICS_TIMEOUT_SECONDS - 2.0
        return read_instance_metrics(client, host, instance_name, project, deadline=deadline)

    async def get_instance_metrics(
        self,
        host: str,
        instance_name: str,
        project: str = "default",
    ) -> InstanceMetrics:
        """Get real-time metrics without blocking the API event loop.

        The pooled pylxd client has its own connect/read socket timeouts. ``run_blocking`` adds
        the wall-clock budget and an in-flight cap so five-second UI polling cannot build an
        unbounded queue when a cluster is degraded.
        """
        return await run_blocking(
            self._get_instance_metrics_blocking,
            host,
            instance_name,
            project,
            timeout=_INSTANCE_METRICS_TIMEOUT_SECONDS,
            label=f"instance.metrics:{host}",
            executor=_instance_metrics_executor,
            max_in_flight=_INSTANCE_METRICS_MAX_IN_FLIGHT,
            concurrency_key=host,
            max_in_flight_per_key=_INSTANCE_METRICS_MAX_IN_FLIGHT_PER_CLUSTER,
        )


# Singleton instance
_lxd_service: Optional[LXDService] = None


def get_lxd_service() -> LXDService:
    """Get or create the LXD service singleton."""
    global _lxd_service
    if _lxd_service is None:
        _lxd_service = LXDService()
    return _lxd_service
