"""
Instance/VM operations module.
Handles create, start, stop, restart, delete, and list operations for instances.
"""

import logging
import asyncio
from typing import List, Tuple, Optional, Dict, Any
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pylxd.exceptions import LXDAPIException

from app.models.schemas import VMInfo, VMStatus
from app.core.config import get_settings
from .exceptions import LXDConnectionError, LXDOperationError
from .utils import parse_vm_status, extract_ip_addresses, find_instance_in_projects
from .resources import ResourceManager
from .connection import LXDConnectionManager

logger = logging.getLogger(__name__)

# Thread pool for parallel instance state fetching
# Enterprise setting: 50 workers for high concurrency
_instance_executor = ThreadPoolExecutor(max_workers=50, thread_name_prefix="instance_state_")

# Cache failed nodes for 60 seconds to avoid repeated reconnection attempts
_failed_nodes_cache: Dict[str, float] = {}
_FAILED_NODE_CACHE_TTL = 60.0


class InstanceManager:
    """
    Manages LXD instance operations.
    """
    
    def __init__(self, connection_manager: LXDConnectionManager, resource_manager: ResourceManager, cert_manager=None):
        self.connection_manager = connection_manager
        self.resource_manager = resource_manager
        self.cert_manager = cert_manager
    
    def _get_client_with_reconnect(self, node_name: str):
        """Get LXD client with automatic reconnection if stale.
        """
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
            profiles=list(instance.profiles) if instance.profiles else [],
            tags=tags,
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
        global _failed_nodes_cache
        vms: List[VMInfo] = []
        
        # Check failed node cache first - skip recently failed nodes
        if node_name in _failed_nodes_cache:
            if time.time() - _failed_nodes_cache[node_name] < _FAILED_NODE_CACHE_TTL:
                logger.debug(f"Skipping {node_name} - recently failed (cached)")
                return [], False
            else:
                del _failed_nodes_cache[node_name]
        
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
                except Exception as e:
                    logger.warning(f"Reconnect failed for {node_name}: {e}")
                    _failed_nodes_cache[node_name] = time.time()
                    return [], False
            
            if client is None:
                _failed_nodes_cache[node_name] = time.time()
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
                                profiles=instance_data.get('profiles', []),
                                tags=tags,
                                project=project_name,
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
                raise LXDOperationError(f"Failed to connect to {host}: connection not available")
            endpoint = self._get_node_endpoint(host)
            
            instance, project_name = find_instance_in_projects(client, name)
            
            if instance is None:
                return None
            
            vm_info = self._instance_to_vm_info(instance, host, endpoint)
            if vm_info.config is None:
                vm_info.config = {}
            vm_info.config['project'] = project_name
            
            return vm_info
            
        except Exception as e:
            logger.error(f"Error getting VM {name} from {host}: {e}")
            raise LXDOperationError(f"Failed to get VM: {str(e)}")
    
    async def start_vm(self, name: str, host: str, force: bool = False) -> VMInfo:
        """Start a VM (searches all projects)."""
        try:
            client = self._get_client_with_reconnect(host)
            if client is None:
                raise LXDOperationError(f"Failed to connect to {host}: connection not available")
            endpoint = self._get_node_endpoint(host)
            
            instance, project_name = find_instance_in_projects(client, name)
            
            if instance is None:
                raise LXDOperationError(f"VM {name} not found on {host}")
            
            if instance.status == "Running":
                vm_info = self._instance_to_vm_info(instance, host, endpoint)
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
    
    async def stop_vm(
        self, 
        name: str, 
        host: str, 
        force: bool = False,
        timeout: int = 30
    ) -> VMInfo:
        """Stop a VM (searches all projects)."""
        try:
            client = self._get_client_with_reconnect(host)
            if client is None:
                raise LXDOperationError(f"Failed to connect to {host}: connection not available")
            endpoint = self._get_node_endpoint(host)
            
            instance, project_name = find_instance_in_projects(client, name)
            
            if instance is None:
                raise LXDOperationError(f"VM {name} not found on {host}")
            
            if instance.status == "Stopped":
                vm_info = self._instance_to_vm_info(instance, host, endpoint)
            else:
                instance.stop(force=force, wait=True, timeout=timeout)
                instance.sync()
                logger.info(f"Stopped VM {name} on {host} (project: {project_name})")
                vm_info = self._instance_to_vm_info(instance, host, endpoint)
            
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
            client = self._get_client_with_reconnect(host)
            if client is None:
                raise LXDOperationError(f"Failed to connect to {host}: connection not available")
            endpoint = self._get_node_endpoint(host)
            
            instance, project_name = find_instance_in_projects(client, name)
            
            if instance is None:
                raise LXDOperationError(f"VM {name} not found on {host}")
            
            instance.restart(force=force, wait=True, timeout=timeout)
            instance.sync()
            
            logger.info(f"Restarted VM {name} on {host} (project: {project_name})")
            vm_info = self._instance_to_vm_info(instance, host, endpoint)
            
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
            client = self._get_client_with_reconnect(host)
            if client is None:
                raise LXDOperationError(f"Failed to connect to {host}: connection not available")
            
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
            client = self._get_client_with_reconnect(host)
            if client is None:
                raise LXDOperationError(f"Failed to connect to {host}: connection not available")
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
