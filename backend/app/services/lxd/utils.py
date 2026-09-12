"""
Utility functions for LXD operations.
Includes helpers for parsing status, extracting IPs, and finding instances.
"""

import logging
from typing import Any, List, Tuple, Optional
import pylxd
from pylxd import exceptions as pylxd_exceptions

from app.models.schemas import VMStatus
from app.core.lxd_names import safe_segment

logger = logging.getLogger(__name__)


def normalize_cluster_member(value: Any) -> Optional[str]:
    """Normalize an LXD member/location field to a member name or None.

    A standalone (unclustered) host reports its instance location as the string "none", not
    as null, so callers that compare locations must treat "none" and "" as "no cluster
    member" or they silently drop every instance on a standalone host.
    """
    text = (value or "").strip()
    if not text or text.lower() == "none":
        return None
    return text


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
            response = client.api.instances[safe_segment(name)].get(params={'project': project_name})
            
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


# important: one blocking wait on the pooled client dies at its 8 s read timeout however long
# the caller asked for (see connection.DEFAULT_LXD_TIMEOUT), so a bounded wait is taken in
# slices shorter than that and the operation is re-polled until it settles or the deadline
# passes. A slice that returns with the operation still Running is not a failure.
OPERATION_WAIT_SLICE_SECONDS = 5
_TERMINAL_OPERATION_STATUSES = {"Success", "Done", "Failure", "Cancelled"}


def wait_for_lxd_operation(
    client: pylxd.Client,
    response: object,
    timeout: Optional[int] = None,
) -> None:
    """Wait for an async LXD operation response to complete.

    A synchronous response (no operation in the body) returns at once, so a write can be
    wrapped unconditionally: LXD 6.9 with the storage_and_network_operations extension
    answers 202 with an operation for network, ACL and storage pool writes that older
    daemons complete before answering.
    """
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

    operation = client.api.operations[safe_segment(operation_id)]
    wait_endpoint = operation.wait
    if timeout is None:
        wait_response = wait_endpoint.get()
    else:
        remaining = float(timeout)
        while True:
            slice_seconds = max(1, int(min(OPERATION_WAIT_SLICE_SECONDS, remaining)))
            try:
                wait_response = wait_endpoint.get(params={"timeout": slice_seconds})
            except pylxd_exceptions.LXDAPIException as exc:
                # important: LXD answers an elapsed wait timeout with an error body, "context
                # deadline exceeded", in the same shape as a failed request, while the operation
                # keeps running. Read the operation itself before believing the error: a device
                # write on a clustered OVN instance takes longer than one slice and was reported
                # as refused while the cluster applied it.
                wait_response = _operation_record(operation, exc)
            remaining -= slice_seconds
            status = _operation_status(wait_response)
            if status in _TERMINAL_OPERATION_STATUSES:
                break
            if remaining <= 0:
                raise pylxd_exceptions.LXDAPIException(_SyntheticResponse(
                    f"Operation {operation_id} is still {status or 'running'} after {timeout}s"
                ))

    try:
        wait_metadata = wait_response.json().get("metadata", {})
    except Exception:
        return

    status = wait_metadata.get("status") if isinstance(wait_metadata, dict) else None
    err = wait_metadata.get("err") if isinstance(wait_metadata, dict) else None
    if err or (status and status not in {"Success", "Done"}):
        raise pylxd_exceptions.LXDAPIException(wait_response)


def _operation_record(operation: object, wait_error: Exception) -> object:
    """The operation as LXD holds it now, or the wait's own error if it cannot be read."""
    try:
        return operation.get()
    except pylxd_exceptions.LXDAPIException:
        raise wait_error


class _SyntheticResponse:
    """An LXD-shaped error body for a deadline of our own, so callers see one exception type."""

    status_code = 200

    def __init__(self, error: str) -> None:
        self._body = {"type": "error", "error": error, "metadata": None}

    def json(self) -> dict:
        return self._body


def _operation_status(wait_response: object) -> Optional[str]:
    try:
        metadata = wait_response.json().get("metadata", {})
    except Exception:
        return None
    return metadata.get("status") if isinstance(metadata, dict) else None


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
        # note: carried through so a root disk size chosen at create time is readable back.
        # The LXD instance payload always includes both; missing means an older LXD.
        self.devices = data.get('devices', {})
        self.expanded_devices = data.get('expanded_devices', {})
        self.profiles = data.get('profiles', [])
    
    def start(self, wait=True):
        """Start the instance."""
        response = self._client.api.instances[safe_segment(self.name)].state.put(
            json={'action': 'start'},
            params={'project': self._project}
        )
        if wait:
            wait_for_lxd_operation(self._client, response)
    
    def stop(self, force=False, wait=True, timeout=30):
        """Stop the instance."""
        response = self._client.api.instances[safe_segment(self.name)].state.put(
            json={'action': 'stop', 'force': force, 'timeout': timeout},
            params={'project': self._project}
        )
        if wait:
            wait_for_lxd_operation(self._client, response, timeout=timeout)
    
    def restart(self, force=False, wait=True, timeout=30):
        """Restart the instance."""
        response = self._client.api.instances[safe_segment(self.name)].state.put(
            json={'action': 'restart', 'force': force, 'timeout': timeout},
            params={'project': self._project}
        )
        if wait:
            wait_for_lxd_operation(self._client, response, timeout=timeout)
    
    def delete(self, wait=True):
        """Delete the instance."""
        response = self._client.api.instances[safe_segment(self.name)].delete(params={'project': self._project})
        if wait:
            wait_for_lxd_operation(self._client, response)
    
    def sync(self):
        """Sync instance data from LXD."""
        response = self._client.api.instances[safe_segment(self.name)].get(params={'project': self._project})
        self._data = response.json()['metadata']
        self.status = self._data.get('status', 'Unknown')
        self.status_code = self._data.get('status_code', 0)
    
    def state(self):
        """Get instance state including metrics."""
        response = self._client.api.instances[safe_segment(self.name)].state.get(params={'project': self._project})
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


def jsonable_lxd(value: Any) -> Any:
    """Recursively convert a pylxd model into something FastAPI can serialise.

    important: pylxd returns dict SUBCLASSES (``model.AttributeDict``) and slot-based objects.
    Pydantic's serialiser does not recognise either, and the failure happens AFTER the handler
    returns, while the response is being rendered, so the handler's own try/except never sees
    it. The caller gets a bare 500 with `PydanticSerializationError: Unable to serialize unknown
    type` in the log and nothing useful in the body.

    Found live on `GET /networks/{name}/state`, which returned `pylxd.models.network.NetworkState`
    straight out of `network.state()`. `routers/storage_pools.py` already carries a
    single-purpose version of this for `StorageResources`, so this is the shared form.
    """
    if isinstance(value, dict):
        return {k: jsonable_lxd(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable_lxd(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    # Slot-based pylxd models expose their fields through marshall() or __slots__.
    marshall = getattr(value, "marshall", None)
    if callable(marshall):
        try:
            marshalled = marshall()
            if marshalled:
                return jsonable_lxd(marshalled)
        except Exception:  # noqa: BLE001 - fall through to the slot walk below
            pass
    slots = getattr(type(value), "__slots__", None)
    if slots:
        return {s: jsonable_lxd(getattr(value, s, None)) for s in slots if not s.startswith("_")}
    # pylxd's AttributeDict is NOT a dict subclass despite the name: it is a plain object that
    # sets each key as an attribute, so its fields live in __dict__ and nowhere else. Checking
    # this last means a real model with slots or marshall() is preferred over its instance dict.
    instance_dict = getattr(value, "__dict__", None)
    if isinstance(instance_dict, dict) and instance_dict:
        return {k: jsonable_lxd(v) for k, v in instance_dict.items() if not k.startswith("_")}
    return str(value)
