"""
Instance/VM operations module.
Handles create, start, stop, restart, delete, and list operations for instances.
"""

import logging
from typing import List, Tuple, Optional, Dict, Any
import time
from pylxd.exceptions import LXDAPIException

from app.models.schemas import VMInfo, VMStatus
from .exceptions import LXDConnectionError, LXDOperationError
from .utils import parse_vm_status, extract_ip_addresses, find_instance_in_projects
from .resources import ResourceManager
from .connection import LXDConnectionManager

logger = logging.getLogger(__name__)


class InstanceManager:
    """
    Manages LXD instance operations.
    """
    
    def __init__(self, connection_manager: LXDConnectionManager, resource_manager: ResourceManager):
        self.connection_manager = connection_manager
        self.resource_manager = resource_manager
    
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
            profiles=list(instance.profiles) if instance.profiles else [],
            tags=tags,
        )
    
    async def list_all_vms(self) -> Tuple[List[VMInfo], List[str]]:
        """
        List all VMs across all configured LXD nodes and all projects.
        Returns tuple of (vm_list, connected_nodes).
        Includes retry logic for transient connection failures.
        """
        all_vms: List[VMInfo] = []
        connected_nodes: List[str] = []
        
        node_configs = {
            name: self.connection_manager.get_node_config(name)
            for name in self.connection_manager.get_node_names()
        }
        
        for node_name, config in node_configs.items():
            retry_count = 0
            max_retries = 2
            
            while retry_count <= max_retries:
                try:
                    client = self.connection_manager.get_client(node_name)
                    connected_nodes.append(node_name)
                    break
                except LXDConnectionError as e:
                    retry_count += 1
                    if retry_count > max_retries:
                        logger.warning(f"Could not connect to node {node_name} after {max_retries} retries: {e}")
                        break
                    logger.info(f"Retrying connection to {node_name} ({retry_count}/{max_retries})...")
                    self.connection_manager.cleanup_connection(node_name)
                    time.sleep(0.5 * retry_count)
                    continue
            
            if node_name not in connected_nodes:
                continue
            
            try:
                client = self.connection_manager.get_client(node_name)
                
                # Get all projects on this node
                try:
                    projects = client.projects.all()
                    project_names = [p.name for p in projects]
                    logger.info(f"Found {len(project_names)} projects on {node_name}: {project_names}")
                except Exception as e:
                    logger.warning(f"Could not list projects on {node_name}, using default: {e}")
                    project_names = ["default"]
                
                # Iterate through all projects
                for project_name in project_names:
                    try:
                        # Get instances list for this project
                        response = client.api.instances.get(params={'project': project_name, 'recursion': 1})
                        instances_data = response.json()['metadata']
                        
                        logger.debug(f"Found {len(instances_data)} instances in project '{project_name}' on {node_name}")
                        
                        for instance_data in instances_data:
                            try:
                                # Parse tags from config
                                config_data = instance_data.get('config', {})
                                tags_str = config_data.get('user.tags', '')
                                tags = [tag.strip() for tag in tags_str.split(',') if tag.strip()] if tags_str else []
                                
                                instance_name = instance_data.get('name', '')
                                instance_status = instance_data.get('status', 'Unknown')
                                
                                # Get state and extract IP addresses
                                ip_addrs = []
                                if instance_status == 'Running':
                                    try:
                                        # Fetch state separately for this instance
                                        state_resp = client.api.instances[instance_name].state.get(params={'project': project_name})
                                        state_data = state_resp.json()['metadata']
                                        network_data = state_data.get('network', {})
                                        
                                        for iface_name, iface_data in network_data.items():
                                            addresses = iface_data.get('addresses', [])
                                            for addr in addresses:
                                                family = addr.get('family')
                                                address = addr.get('address')
                                                scope = addr.get('scope', '')
                                                # Include IPv4 and global IPv6
                                                if family == 'inet' or (family == 'inet6' and scope == 'global'):
                                                    if address:
                                                        ip_addrs.append(address)
                                    except Exception as e:
                                        logger.warning(f"Could not get state for {instance_name}: {e}")
                                
                                vm_info = VMInfo(
                                    name=instance_name,
                                    status=parse_vm_status(
                                        instance_status,
                                        instance_data.get('status_code', 0)
                                    ),
                                    status_code=instance_data.get('status_code', 0),
                                    ip_addresses=ip_addrs,
                                    host=node_name,
                                    host_endpoint=config.endpoint,
                                    architecture=instance_data.get('architecture', ''),
                                    created_at=instance_data.get('created_at'),
                                    last_used_at=instance_data.get('last_used_at'),
                                    description=instance_data.get('description', ''),
                                    type=instance_data.get('type', 'container'),
                                    resources=None,
                                    config=config_data,
                                    profiles=instance_data.get('profiles', []),
                                    tags=tags,
                                    project=project_name,
                                )
                                
                                if vm_info.config is None:
                                    vm_info.config = {}
                                vm_info.config['project'] = project_name
                                
                                all_vms.append(vm_info)
                            except Exception as e:
                                logger.error(
                                    f"Error processing instance in project '{project_name}' on {node_name}: {e}"
                                )
                    except Exception as e:
                        logger.error(f"Error listing instances in project '{project_name}' on {node_name}: {e}")
                        
            except LXDConnectionError as e:
                logger.warning(f"Could not connect to node {node_name}: {e}")
            except Exception as e:
                logger.error(f"Error listing VMs from {node_name}: {e}")
        
        return all_vms, connected_nodes
    
    async def get_vm(self, name: str, host: str) -> Optional[VMInfo]:
        """Get a specific VM by name and host (searches all projects)."""
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                client = self.connection_manager.get_client(host)
                config = self.connection_manager.get_node_config(host)
                break
            except LXDConnectionError as e:
                if attempt == max_retries:
                    raise LXDOperationError(f"Failed to connect to {host} after {max_retries} retries: {e}")
                logger.info(f"Retrying get_vm connection to {host} ({attempt + 1}/{max_retries})...")
                self.connection_manager.cleanup_connection(host)
                time.sleep(0.5)
                continue
        
        try:
            client = self.connection_manager.get_client(host)
            config = self.connection_manager.get_node_config(host)
            
            instance, project_name = find_instance_in_projects(client, name)
            
            if instance is None:
                return None
            
            vm_info = self._instance_to_vm_info(instance, host, config.endpoint)
            if vm_info.config is None:
                vm_info.config = {}
            vm_info.config['project'] = project_name
            
            return vm_info
            
        except Exception as e:
            logger.error(f"Error getting VM {name} from {host}: {e}")
            raise LXDOperationError(f"Failed to get VM: {str(e)}")
    
    async def start_vm(self, name: str, host: str, force: bool = False) -> VMInfo:
        """Start a VM (searches all projects)."""
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                client = self.connection_manager.get_client(host)
                config = self.connection_manager.get_node_config(host)
                break
            except LXDConnectionError as e:
                if attempt == max_retries:
                    raise LXDOperationError(f"Failed to connect to {host}: {e}")
                logger.info(f"Retrying start_vm connection to {host} ({attempt + 1}/{max_retries})...")
                self.connection_manager.cleanup_connection(host)
                time.sleep(0.5)
                continue
        
        try:
            client = self.connection_manager.get_client(host)
            config = self.connection_manager.get_node_config(host)
            
            instance, project_name = find_instance_in_projects(client, name)
            
            if instance is None:
                raise LXDOperationError(f"VM {name} not found on {host}")
            
            if instance.status == "Running":
                vm_info = self._instance_to_vm_info(instance, host, config.endpoint)
            else:
                instance.start(wait=True)
                instance.sync()
                logger.info(f"Started VM {name} on {host} (project: {project_name})")
                vm_info = self._instance_to_vm_info(instance, host, config.endpoint)
            
            if vm_info.config is None:
                vm_info.config = {}
            vm_info.config['project'] = project_name
            
            return vm_info
            
        except LXDAPIException as e:
            raise LXDOperationError(f"Failed to start VM: {str(e)}")
    
    async def stop_vm(
        self, 
        name: str, 
        host: str, 
        force: bool = False,
        timeout: int = 30
    ) -> VMInfo:
        """Stop a VM (searches all projects)."""
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                client = self.connection_manager.get_client(host)
                config = self.connection_manager.get_node_config(host)
                break
            except LXDConnectionError as e:
                if attempt == max_retries:
                    raise LXDOperationError(f"Failed to connect to {host}: {e}")
                logger.info(f"Retrying stop_vm connection to {host} ({attempt + 1}/{max_retries})...")
                self.connection_manager.cleanup_connection(host)
                time.sleep(0.5)
                continue
        
        try:
            client = self.connection_manager.get_client(host)
            config = self.connection_manager.get_node_config(host)
            
            instance, project_name = find_instance_in_projects(client, name)
            
            if instance is None:
                raise LXDOperationError(f"VM {name} not found on {host}")
            
            if instance.status == "Stopped":
                vm_info = self._instance_to_vm_info(instance, host, config.endpoint)
            else:
                instance.stop(force=force, wait=True, timeout=timeout)
                instance.sync()
                logger.info(f"Stopped VM {name} on {host} (project: {project_name})")
                vm_info = self._instance_to_vm_info(instance, host, config.endpoint)
            
            if vm_info.config is None:
                vm_info.config = {}
            vm_info.config['project'] = project_name
            
            return vm_info
            
        except LXDAPIException as e:
            raise LXDOperationError(f"Failed to stop VM: {str(e)}")
    
    async def restart_vm(
        self, 
        name: str, 
        host: str, 
        force: bool = False,
        timeout: int = 30
    ) -> VMInfo:
        """Restart a VM (searches all projects)."""
        try:
            client = self.connection_manager.get_client(host)
            config = self.connection_manager.get_node_config(host)
            
            instance, project_name = find_instance_in_projects(client, name)
            
            if instance is None:
                raise LXDOperationError(f"VM {name} not found on {host}")
            
            instance.restart(force=force, wait=True, timeout=timeout)
            instance.sync()
            
            logger.info(f"Restarted VM {name} on {host} (project: {project_name})")
            vm_info = self._instance_to_vm_info(instance, host, config.endpoint)
            
            if vm_info.config is None:
                vm_info.config = {}
            vm_info.config['project'] = project_name
            
            return vm_info
            
        except LXDAPIException as e:
            raise LXDOperationError(f"Failed to restart VM: {str(e)}")
    
    async def delete_vm(
        self, 
        name: str, 
        host: str, 
        force: bool = False
    ) -> bool:
        """Delete a VM (searches all projects)."""
        try:
            client = self.connection_manager.get_client(host)
            
            instance, project_name = find_instance_in_projects(client, name)
            
            if instance is None:
                raise LXDOperationError(f"VM {name} not found on {host}")
            
            # Stop if running and force is True
            if instance.status == "Running":
                if force:
                    instance.stop(force=True, wait=True)
                else:
                    raise LXDOperationError(
                        f"VM {name} is running. Stop it first or use force=true"
                    )
            
            instance.delete(wait=True)
            
            logger.info(f"Deleted VM {name} from {host} (project: {project_name})")
            return True
            
        except LXDAPIException as e:
            raise LXDOperationError(f"Failed to delete VM: {str(e)}")
    
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
            client = self.connection_manager.get_client(host)
            node_config = self.connection_manager.get_node_config(host)
            
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
            
            return self._instance_to_vm_info(instance, host, node_config.endpoint)
            
        except LXDAPIException as e:
            raise LXDOperationError(f"Failed to create VM: {str(e)}")
