"""
LXD Service - Main integration module.
Provides unified interface for all LXD operations.
"""

import logging
from typing import List, Tuple, Optional, Dict, Any

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

logger = logging.getLogger(__name__)


class LXDService:
    """
    Main service class for managing LXD operations across multiple nodes.
    Provides unified interface for VM management, projects, images, and resources.
    """
    
    def __init__(self):
        # Initialize managers
        self.connection_manager = LXDConnectionManager()
        self.resource_manager = ResourceManager()
        self.instance_manager = InstanceManager(self.connection_manager, self.resource_manager)
        self.project_manager = ProjectManager(self.connection_manager)
        self.image_manager = ImageManager(self.connection_manager)
    
    # Node operations
    def get_node_names(self) -> List[str]:
        """Get list of configured node names."""
        return self.connection_manager.get_node_names()
    
    def check_nodes_status(self) -> List[NodeStatus]:
        """Check connection status for all nodes."""
        statuses = []
        
        for node_name in self.connection_manager.get_node_names():
            config = self.connection_manager.get_node_config(node_name)
            try:
                client = self.connection_manager.get_client(node_name)
                status = self.resource_manager.check_node_status(client, node_name, config.endpoint)
                statuses.append(status)
            except Exception as e:
                status = NodeStatus(
                    name=node_name,
                    endpoint=config.endpoint,
                    connected=False,
                    error=str(e)
                )
                statuses.append(status)
                logger.warning(f"Node {node_name} health check failed: {e}")
        
        return statuses
    
    def get_node_resources(self, node_name: str) -> Dict[str, Any]:
        """Get available resources for a specific node."""
        client = self.connection_manager.get_client(node_name)
        return self.resource_manager.get_node_resources(client, node_name)
    
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
    
    async def start_vm(self, name: str, host: str, force: bool = False) -> VMInfo:
        """Start a VM (searches all projects)."""
        return await self.instance_manager.start_vm(name, host, force)
    
    async def stop_vm(
        self, 
        name: str, 
        host: str, 
        force: bool = False,
        timeout: int = 30
    ) -> VMInfo:
        """Stop a VM (searches all projects)."""
        return await self.instance_manager.stop_vm(name, host, force, timeout)
    
    async def restart_vm(
        self, 
        name: str, 
        host: str, 
        force: bool = False,
        timeout: int = 30
    ) -> VMInfo:
        """Restart a VM (searches all projects)."""
        return await self.instance_manager.restart_vm(name, host, force, timeout)
    
    async def delete_vm(
        self, 
        name: str, 
        host: str, 
        force: bool = False
    ) -> bool:
        """Delete a VM (searches all projects)."""
        return await self.instance_manager.delete_vm(name, host, force)
    
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
        return await self.instance_manager.create_vm(
            name, host, image, instance_type, profiles, config, 
            cloud_init, devices, start_on_create
        )
    
    # Image operations
    async def list_images(self, host: str) -> List[ImageInfo]:
        """List available images on a host."""
        return await self.image_manager.list_images(host)
    
    # Project operations
    async def list_projects(self, host: str) -> List[Dict[str, Any]]:
        """List all projects on a specific LXD host."""
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
