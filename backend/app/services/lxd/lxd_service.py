"""
LXD Service - Main integration module.
Provides unified interface for all LXD operations.
"""

import asyncio
import logging
from typing import List, Tuple, Optional, Dict, Any
from concurrent.futures import ThreadPoolExecutor

from app.models.schemas import (
    VMInfo,
    ImageInfo,
    NodeResources,
    NodeStatus,
    InstanceMetrics,
)
from .availability import require_client
from .connection import LXDConnectionManager
from .gpu import GpuInventory, build_gpu_inventory
from .node_resources import build_node_resources
from .resources import ResourceManager
from .instance import InstanceManager
from .projects import ProjectManager
from .images import ImageManager
from .exceptions import LXDConnectionError
from app.services.lxd_cert_manager import LXDCertificateManager
from app.core.vault_client import VaultClient
from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Shared thread pool for blocking operations
_blocking_executor = ThreadPoolExecutor(max_workers=10, thread_name_prefix="lxd_blocking")

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
    
    def ensure_connection(self, cluster_id: str) -> bool:
        """
        Ensure cluster is connected (lazy loading helper).
        Respects org-scope: returns False if the cluster is outside user's org.
        """
        # Org-scope guard: prevent reconnecting to clusters outside the user's org
        from app.core.org_scope import get_current_allowed_clusters
        from app.services.lxd import health
        from app.services.lxd.exceptions import LXDAuthorizationError
        allowed = get_current_allowed_clusters()
        if allowed is not None and cluster_id not in allowed:
            return False

        # Check if already connected
        client = self.connection_manager.get_client(cluster_id)
        if client:
            return True

        # important: fast-skip clusters the breaker marked as recently failed -
        # this is the project/instance lazy-connect path; without it every
        # fan-out re-pays the dead-cluster connect timeout (the projects stall).
        if health.is_tripped(cluster_id):
            return False

        # Try to connect
        try:
            cluster_info = self.cert_manager.get_cluster_info(cluster_id)
            cert_data = self.cert_manager.get_cluster_certificate(cluster_id)
            
            connected = self.connection_manager.connect_with_vault_cert(
                cluster_id=cluster_id,
                endpoint=cluster_info["metadata"]["endpoint"],
                cert_content=cert_data["client.crt"],
                key_content=cert_data["client.key"],
                verify=False
            )
            # note: this returns None when the breaker refuses it. Treating that as success
            # cleared the breaker and reported a cluster as connected that never was.
            if connected is None:
                return False
            logger.info(f"Lazy-connected to cluster: {cluster_id}")
            health.clear(cluster_id)
            return True
        except LXDAuthorizationError as e:
            # important: the host ANSWERED and refused our certificate. Letting this fall into
            # the broad handler below recorded it as a generic failure, so the breaker's kind
            # reverted to "offline" one line after connect_with_vault_cert had correctly
            # recorded "unauthorized". Five clusters on the dev fleet answered HTTP 200 and
            # still read Offline on every surface because of these two lines.
            logger.warning(f"Cluster {cluster_id} refused our certificate: {e}")
            health.record_failure(cluster_id, str(e), kind=health.KIND_UNAUTHORIZED)
            return False
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

    def _read_endpoint(self, node_name: str, settings) -> str:
        """The cluster's registered endpoint, which does not depend on whether it answers."""
        try:
            metadata = self.cert_manager.vault.vault_read(
                f"{settings.vault_clusters_path}/{node_name}/metadata"
            )
            return metadata.get("endpoint", "unknown")
        except Exception:
            return "unknown"

    def _check_single_node_status(self, node_name: str, settings) -> NodeStatus:
        """Check status for a single node (internal helper)."""
        from app.services.lxd import health

        # important: read the endpoint BEFORE the breaker check. It is registry metadata and has
        # nothing to do with reachability, but returning it only on the healthy path meant every
        # dead cluster reported endpoint="unknown", and a node picker printed the word "unknown"
        # under its name. The breaker exists to skip an 8s connect, not a local Vault read.
        endpoint = self._read_endpoint(node_name, settings)

        # Skip clusters the shared breaker marked as recently failed.
        if health.is_tripped(node_name):
            logger.debug(f"Skipping {node_name} - recently failed (cached)")
            # The breaker remembers WHY it tripped, so a cluster that answered and refused our
            # certificate keeps reading as untrusted instead of degrading to "offline" for the
            # 60s the breaker holds.
            return NodeStatus(
                name=node_name,
                endpoint=endpoint,
                connected=False,
                reachable=health.get_kind(node_name) == health.KIND_UNAUTHORIZED,
                error=health.get_reason(node_name) or "Node recently failed - retry later"
            )

        try:
            
            # Use get_client_with_reconnect for automatic reconnection
            client = self.get_client_with_reconnect(node_name)
            if client:
                status = self.resource_manager.check_node_status(client, node_name, endpoint)
                return status
            else:
                # important: get_client has already recorded WHY it failed, and an untrusted
                # certificate records KIND_UNAUTHORIZED. Overwriting that with a generic
                # "Client not connected" downgrades a cluster that answered and refused us
                # into one that looks powered off, and the breaker then serves that wrong
                # answer to every reader for its whole TTL.
                kind = health.get_kind(node_name)
                if kind is None:
                    health.record_failure(node_name, "Client not connected")
                    kind = health.KIND_OFFLINE
                return NodeStatus(
                    name=node_name,
                    endpoint=endpoint,
                    connected=False,
                    reachable=kind == health.KIND_UNAUTHORIZED,
                    error=health.get_reason(node_name) or "Client not connected"
                )
        except Exception as e:
            logger.warning(f"Node {node_name} health check failed: {e}")
            kind = health.get_kind(node_name)
            if kind is None:
                health.record_failure(node_name, str(e))
                kind = health.KIND_OFFLINE
            return NodeStatus(
                name=node_name,
                endpoint=endpoint,
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
        return self.resource_manager.get_node_resources(client, node_name)

    def get_node_resources_detail(self, node_name: str) -> NodeResources:
        """Full NodeResources for one node, GPU included.

        important: GET /nodes/{node}/resources is typed as NodeResources but was built from
        the CPU/memory/storage dict alone, so every GPU field it declared was served as its
        default. The endpoint reported zero GPUs on hardware that has one.
        """
        client = require_client(self.connection_manager.get_client(node_name), node_name)
        data = self.resource_manager.get_node_resources(client, node_name)
        gpu = GpuInventory.empty()
        try:
            gpu = build_gpu_inventory(client.resources, client.instances.all())
        except Exception as e:
            logger.debug(f"Could not read GPU inventory for {node_name}: {e}")
        return build_node_resources(data, gpu)

    def get_node_gpus(self, node_name: str) -> GpuInventory:
        """List the GPUs on a specific node, with their class and allocation state."""
        client = require_client(self.connection_manager.get_client(node_name), node_name)
        return self.resource_manager.get_node_gpus(client, node_name)
    
    # Instance/VM operations
    async def list_all_vms(self) -> Tuple[List[VMInfo], List[str]]:
        """
        List all VMs across all configured LXD nodes and all projects.
        Returns tuple of (vm_list, connected_nodes).
        """
        return await self.instance_manager.list_all_vms()
    
    async def get_vm(self, name: str, host: str) -> Optional[VMInfo]:
        """Get a specific VM by name and host (searches all projects)."""
        return await self.instance_manager.get_vm(name, host)
    
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
    ) -> bool:
        """Delete a VM (searches all projects)."""
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
    ) -> VMInfo:
        """Create a new VM (blocking). Stable contract for `POST /vms` (Orcahub)."""
        return await self.instance_manager.create_vm(
            name, host, image, instance_type, profiles, config,
            cloud_init, devices, start_on_create
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
        """Start creating a new instance (non-blocking); returns the operation id.

        Backs the dashboard's `POST /instances`. See InstanceManager.create_instance.
        `source_type="none"` creates a blank VM (empty root disk, no OS).
        """
        return await self.instance_manager.create_instance(
            name, host, image, instance_type, profiles, config,
            cloud_init, devices, start_on_create, project,
            image_server, image_protocol, description, source_type,
        )

    # Image operations
    async def list_images(self, host: str) -> List[ImageInfo]:
        """List available images on a host."""
        return await self.image_manager.list_images(host)
    
    # Project operations
    async def list_projects(self, host: str) -> List[Dict[str, Any]]:
        """List all projects on a specific LXD host."""
        # Ensure connection (lazy loading)
        self.ensure_connection(host)
        return await self.project_manager.list_projects(host)
    
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
    async def get_instance_metrics(
        self, 
        host: str, 
        instance_name: str,
        project: str = "default"
    ) -> InstanceMetrics:
        """Get real-time metrics for a specific instance."""
        client = self.connection_manager.get_client(host)
        # important: without this, a None client reaches find_instance_in_projects, which
        # swallows the AttributeError in both of its loops and returns (None, None). The
        # caller then reports "Instance '<name>' not found on <host>" and the router
        # message-sniffs "not found" into a 404 -- telling the operator their instance was
        # deleted when the cluster is merely offline.
        if client is None:
            raise LXDConnectionError(f"Could not connect to {host}")
        return self.resource_manager.get_instance_metrics(client, host, instance_name, project)


# Singleton instance
_lxd_service: Optional[LXDService] = None


def get_lxd_service() -> LXDService:
    """Get or create the LXD service singleton."""
    global _lxd_service
    if _lxd_service is None:
        _lxd_service = LXDService()
    return _lxd_service
