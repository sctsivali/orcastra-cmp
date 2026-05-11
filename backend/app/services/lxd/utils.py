"""
Utility functions for LXD operations.
Includes helpers for parsing status, extracting IPs, and finding instances.
"""

import logging
from typing import List, Tuple, Optional
import pylxd
from pylxd import exceptions as pylxd_exceptions

from app.models.schemas import VMStatus

logger = logging.getLogger(__name__)


def parse_vm_status(status: str, status_code: int) -> VMStatus:
    """Convert LXD status string to VMStatus enum."""
    status_map = {
        "Running": VMStatus.RUNNING,
        "Stopped": VMStatus.STOPPED,
        "Frozen": VMStatus.FROZEN,
        "Error": VMStatus.ERROR,
    }
    return status_map.get(status, VMStatus.UNKNOWN)


def extract_ip_addresses(instance) -> List[str]:
    """Extract IP addresses from instance network state."""
    ip_addresses = []
    
    try:
        if instance.status != "Running":
            return ip_addresses
        
        state = instance.state()
        if state and state.network:
            for iface_name, iface_data in state.network.items():
                if iface_name == "lo":
                    continue
                
                for addr in iface_data.get("addresses", []):
                    if addr.get("family") == "inet":
                        ip_addresses.append(addr.get("address"))
                    elif addr.get("family") == "inet6":
                        ip_addr = addr.get("address")
                        # Skip link-local addresses
                        if not ip_addr.startswith("fe80"):
                            ip_addresses.append(ip_addr)
    except Exception as e:
        logger.debug(f"Could not get IP addresses for {instance.name}: {e}")
    
    return ip_addresses


def extract_ip_addresses_from_state(state_data: dict) -> List[str]:
    """Extract IP addresses from instance state data dictionary."""
    ip_addresses = []
    
    try:
        network_data = state_data.get('network', {})
        if not network_data:
            return ip_addresses
        
        for iface_name, iface_data in network_data.items():
            if iface_name == "lo":
                continue
            
            addresses = iface_data.get('addresses', [])
            for addr in addresses:
                if addr.get('family') == 'inet':
                    ip_addresses.append(addr.get('address'))
                elif addr.get('family') == 'inet6':
                    ip_addr = addr.get('address')
                    # Skip link-local addresses
                    if ip_addr and not ip_addr.startswith('fe80'):
                        ip_addresses.append(ip_addr)
    except Exception as e:
        logger.debug(f"Could not extract IP addresses from state data: {e}")
    
    return ip_addresses


def find_instance_in_projects(
    client: pylxd.Client,
    name: str,
    project: Optional[str] = None,
) -> Tuple[Optional[object], Optional[str]]:
    """
    Find an instance by name across all projects.
    Returns tuple of (instance, project_name) or (None, None) if not found.
    """
    if project:
        project_names = [project]
    else:
        try:
            projects = client.projects.all()
            project_names = [p.name for p in projects]
        except Exception as e:
            logger.warning(f"Could not list projects, using default: {e}")
            project_names = ["default"]
    
    # Search in each project
    for project_name in project_names:
        try:
            # Try to get instance from this project using API directly
            response = client.api.instances[name].get(params={'project': project_name})
            
            # If successful, create instance object from the response
            instance_data = response.json()['metadata']
            
            # Create a wrapper class with the data we need
            instance = InstanceWrapper(instance_data, client, project_name)
            logger.debug(f"Found instance '{name}' in project '{project_name}'")
            return instance, project_name
            
        except Exception as e:
            # Instance not found in this project or other error
            logger.debug(f"Instance '{name}' not in project '{project_name}': {e}")
            continue
    
    return None, None


def wait_for_lxd_operation(
    client: pylxd.Client,
    response: object,
    timeout: Optional[int] = None,
) -> None:
    """Wait for an async LXD operation response to complete."""
    try:
        response_data = response.json()
    except Exception:
        return

    if not isinstance(response_data, dict):
        return

    operation_id = None
    operation_path = response_data.get("operation")
    metadata = response_data.get("metadata")

    if isinstance(operation_path, str) and operation_path:
        operation_id = operation_path.rstrip("/").split("/")[-1]
    elif isinstance(metadata, dict):
        operation_id = metadata.get("id")

    if not operation_id:
        return

    if timeout is not None:
        wait_response = client.api.operations[operation_id].wait.get(
            params={"timeout": timeout}
        )
    else:
        wait_response = client.api.operations[operation_id].wait.get()

    try:
        wait_metadata = wait_response.json().get("metadata", {})
    except Exception:
        return

    status = wait_metadata.get("status") if isinstance(wait_metadata, dict) else None
    err = wait_metadata.get("err") if isinstance(wait_metadata, dict) else None
    if err or (status and status not in {"Success", "Done"}):
        raise pylxd_exceptions.LXDAPIException(wait_response)


class InstanceWrapper:
    """
    Wrapper class for LXD instance that works across projects.
    Provides a unified interface for instance operations.
    """
    
    def __init__(self, data, client, project):
        self._data = data
        self._client = client
        self._project = project
        self.name = data.get('name', '')
        self.status = data.get('status', 'Unknown')
        self.status_code = data.get('status_code', 0)
        self.architecture = data.get('architecture', '')
        self.created_at = data.get('created_at')
        self.last_used_at = data.get('last_used_at')
        self.description = data.get('description', '')
        self.type = data.get('type', 'container')
        self.config = data.get('config', {})
        self.profiles = data.get('profiles', [])
    
    def start(self, wait=True):
        """Start the instance."""
        response = self._client.api.instances[self.name].state.put(
            json={'action': 'start'},
            params={'project': self._project}
        )
        if wait:
            wait_for_lxd_operation(self._client, response)
    
    def stop(self, force=False, wait=True, timeout=30):
        """Stop the instance."""
        response = self._client.api.instances[self.name].state.put(
            json={'action': 'stop', 'force': force, 'timeout': timeout},
            params={'project': self._project}
        )
        if wait:
            wait_for_lxd_operation(self._client, response, timeout=timeout)
    
    def restart(self, force=False, wait=True, timeout=30):
        """Restart the instance."""
        response = self._client.api.instances[self.name].state.put(
            json={'action': 'restart', 'force': force, 'timeout': timeout},
            params={'project': self._project}
        )
        if wait:
            wait_for_lxd_operation(self._client, response, timeout=timeout)
    
    def delete(self, wait=True):
        """Delete the instance."""
        response = self._client.api.instances[self.name].delete(params={'project': self._project})
        if wait:
            wait_for_lxd_operation(self._client, response)
    
    def sync(self):
        """Sync instance data from LXD."""
        response = self._client.api.instances[self.name].get(params={'project': self._project})
        self._data = response.json()['metadata']
        self.status = self._data.get('status', 'Unknown')
        self.status_code = self._data.get('status_code', 0)
    
    def state(self):
        """Get instance state including metrics."""
        response = self._client.api.instances[self.name].state.get(params={'project': self._project})
        state_data = response.json()['metadata']
        
        # Create a simple object to hold state data
        return StateWrapper(state_data)


class StateWrapper:
    """
    Wrapper class for instance state data.
    """
    
    def __init__(self, data):
        self.cpu = data.get('cpu', {})
        self.memory = data.get('memory', {})
        self.disk = data.get('disk', {})
        self.network = data.get('network', {})
        self.processes = data.get('processes', 0)
        self.pid = data.get('pid', 0)
        self.status = data.get('status', 'Unknown')
        self.started_at = data.get('started_at')
