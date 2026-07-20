"""
LXD Service - Main integration module.
Provides unified interface for all LXD operations.
"""

import asyncio
import logging
import time
from typing import List, Tuple, Optional, Dict, Any
from concurrent.futures import ThreadPoolExecutor

from app.models.schemas import (
    VMInfo,
    ImageInfo,
    NodeStatus,
    InstanceMetrics,
)
from .connection import LXDConnectionManager
from .resources import ResourceManager
from .instance import InstanceManager
from .projects import ProjectManager
from .images import ImageManager
from .exceptions import LXDConnectionError, LXDOperationError
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
            
            self.connection_manager.connect_with_vault_cert(
                cluster_id=cluster_id,
                endpoint=cluster_info["metadata"]["endpoint"],
                cert_content=cert_data["client.crt"],
                key_content=cert_data["client.key"],
                verify=False
            )
            logger.info(f"Lazy-connected to cluster: {cluster_id}")
            health.clear(cluster_id)
            return True
        except Exception as e:
            logger.error(f"Failed to connect cluster {cluster_id}: {e}")
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
    
    def _check_single_node_status(self, node_name: str, settings) -> NodeStatus:
        """Check status for a single node (internal helper)."""
        from app.services.lxd import health

        # Skip clusters the shared breaker marked as recently failed.
        if health.is_tripped(node_name):
            logger.debug(f"Skipping {node_name} - recently failed (cached)")
            return NodeStatus(
                name=node_name,
                endpoint="unknown",
                connected=False,
                error=health.get_reason(node_name) or "Node recently failed - retry later"
            )

        try:
            # Get endpoint from Vault metadata
            endpoint = "unknown"
            try:
                metadata = self.cert_manager.vault.vault_read(
                    f"{settings.vault_clusters_path}/{node_name}/metadata"
                )
                endpoint = metadata.get("endpoint", "unknown")
            except Exception:
                pass
            
            # Use get_client_with_reconnect for automatic reconnection
            client = self.get_client_with_reconnect(node_name)
            if client:
                status = self.resource_manager.check_node_status(client, node_name, endpoint)
                return status
            else:
                health.record_failure(node_name, "Client not connected")
                return NodeStatus(
                    name=node_name,
                    endpoint=endpoint,
                    connected=False,
                    error="Client not connected"
                )
        except Exception as e:
            logger.warning(f"Node {node_name} health check failed: {e}")
            health.record_failure(node_name, str(e))
            return NodeStatus(
                name=node_name,
                endpoint="unknown",
                connected=False,
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
                    error=str(status)
                ))
            else:
                result.append(status)
        
        return result
        
        return statuses
    
    def get_node_resources(self, node_name: str) -> Dict[str, Any]:
        """Get available resources for a specific node."""
        client = self.connection_manager.get_client(node_name)
        return self.resource_manager.get_node_resources(client, node_name)

    def get_node_gpus(self, node_name: str) -> List[Dict[str, Any]]:
        """List the physical GPUs on a specific node."""
        client = self.connection_manager.get_client(node_name)
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
        return self.resource_manager.get_instance_metrics(client, host, instance_name, project)


# Singleton instance
_lxd_service: Optional[LXDService] = None


def get_lxd_service() -> LXDService:
    """Get or create the LXD service singleton."""
    global _lxd_service
    if _lxd_service is None:
        _lxd_service = LXDService()
    return _lxd_service
