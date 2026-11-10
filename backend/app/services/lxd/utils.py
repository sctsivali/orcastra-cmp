"""Helpers for LXD calls: status parsing, IP extraction, instance lookup, operation waits."""

import ipaddress
import logging
from typing import Any, Callable, Dict, List, Tuple, Optional
import pylxd
from pylxd import exceptions as pylxd_exceptions
from requests import exceptions as requests_exceptions

from app.models.schemas import VMStatus
from app.core.lxd_path import safe_segment
from app.core.lxd_segment import lxd_segment
from app.core.member_names import is_standalone_location

logger = logging.getLogger(__name__)


def normalize_cluster_member(value: Any) -> Optional[str]:
    """Normalize an LXD member/location field to a member name or None.

    A standalone (unclustered) host reports its instance location as the string "none", not
    as null, so callers that compare locations must treat "none" and "" as "no cluster
    member" or they silently drop every instance on a standalone host.
    """
    if is_standalone_location(value):
        return None
    return str(value).strip()


def parse_vm_status(status: str, status_code: int) -> VMStatus:
    """Convert LXD status string to VMStatus enum."""
    status_map = {
        "Running": VMStatus.RUNNING,
        "Stopped": VMStatus.STOPPED,
        "Frozen": VMStatus.FROZEN,
        "Error": VMStatus.ERROR,
    }
    return status_map.get(status, VMStatus.UNKNOWN)


def image_source_key(image: str) -> str:
    """
    "fingerprint" for a full fingerprint or the prefix `lxc image list` prints, else "alias".

    LXD resolves a fingerprint prefix itself, but only when it is sent as one; sent as an
    alias, the 12 characters an operator pastes from the image list are "not found".
    """
    ref = image or ""
    if 12 <= len(ref) <= 64 and all(c in "0123456789abcdef" for c in ref):
        return "fingerprint"
    return "alias"


def is_display_address(addr: Dict[str, Any]) -> bool:
    """
    Whether one address from an instance's network state belongs in an IP column.

    Skipping `lo` by name is not enough (a loopback address can live on another interface),
    so the address itself decides: reachable from outside the instance, so not loopback, not
    link-local, and not scope "local" or "link".
    """
    address = addr.get("address") or ""
    family = addr.get("family")
    if not address or family not in ("inet", "inet6"):
        return False
    if addr.get("scope") in ("local", "link"):
        return False
    try:
        parsed = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return not address.startswith(("127.", "fe80", "::1"))
    return not (parsed.is_loopback or parsed.is_link_local)


def _display_addresses(network_data: Dict[str, Any]) -> List[str]:
    return [
        addr.get("address")
        for iface_name, iface_data in (network_data or {}).items()
        if iface_name != "lo"
        for addr in (iface_data or {}).get("addresses", [])
        if is_display_address(addr)
    ]


def extract_ip_addresses(instance) -> List[str]:
    """Extract IP addresses from instance network state."""
    try:
        if instance.status != "Running":
            return []
        state = instance.state()
        if state and state.network:
            return _display_addresses(state.network)
    except Exception as e:
        logger.debug(f"Could not get IP addresses for {instance.name}: {e}")
    return []


def extract_ip_addresses_from_state(state_data: dict) -> List[str]:
    """Extract IP addresses from instance state data dictionary."""
    try:
        return _display_addresses(state_data.get('network', {}))
    except Exception as e:
        logger.debug(f"Could not extract IP addresses from state data: {e}")
    return []


def find_instance_in_projects(
    client: pylxd.Client,
    name: str,
    project: Optional[str] = None,
    *,
    project_filter: Optional[Callable[[str], bool]] = None,
) -> Tuple[Optional[object], Optional[str]]:
    """
    Find an instance by name across all projects.
    Returns tuple of (instance, project_name) or (None, None) if not found.
    """
    # important: `name` is the caller's, so an empty, `.` or `..` name is refused with 400 before any
    # request, the project list included (core/lxd_segment.py). requests folds a dot segment out of
    # the path, so `..` would read LXD's server root and answer its config as an instance.
    lxd_segment(name, what="Instance name")
    if project:
        project_names = [project]
    else:
        try:
            projects = client.projects.all()
            project_names = [p.name for p in projects]
        except Exception as e:
            logger.warning(f"Could not list projects, using default: {e}")
            project_names = ["default"]
    
    # Search only projects the caller authorized. This predicate is evaluated before the HTTP
    # GET, not after a match, so a detail request without `project` cannot probe a neighbour's
    # project and then attempt to hide the result after it has already been read.
    if project_filter is not None:
        project_names = [name for name in project_names if project_filter(name)]

    for project_name in project_names:
        try:
            # The raw API, so the request names this project.
            response = client.api.instances[lxd_segment(name, what="Instance name")].get(params={'project': project_name})
            
            instance_data = response.json()['metadata']
            
            instance = InstanceWrapper(instance_data, client, project_name)
            logger.debug(f"Found instance '{name}' in project '{project_name}'")
            return instance, project_name
            
        except pylxd_exceptions.NotFound:
            logger.debug("Instance '%s' not in project '%s'", name, project_name)
            continue
        except pylxd_exceptions.LXDAPIException as exc:
            if getattr(getattr(exc, "response", None), "status_code", None) == 404:
                logger.debug("Instance '%s' not in project '%s'", name, project_name)
                continue
            raise
        except requests_exceptions.HTTPError as exc:
            if getattr(getattr(exc, "response", None), "status_code", None) == 404:
                logger.debug("Instance '%s' not in project '%s'", name, project_name)
                continue
            raise
        except RuntimeError as exc:
            # Some pylxd-compatible adapters raise a bare RuntimeError("not found") for an
            # absent record. Only that exact message counts as absent: a broader catch would
            # swallow socket timeouts and report an outage as a missing VM.
            if str(exc).strip().lower() == "not found":
                logger.debug("Instance '%s' not in project '%s'", name, project_name)
                continue
            raise
    
    return None, None


# important: one blocking wait on the pooled client dies at its 8 s read timeout however long
# the caller asked for (see connection.DEFAULT_LXD_TIMEOUT), so a bounded wait is taken in
# slices shorter than that and the operation is re-polled until it settles or the deadline
# passes. A slice that returns with the operation still Running is not a failure.
OPERATION_WAIT_SLICE_SECONDS = 5
# The pooled client reads for 8 s. A daemon without storage_and_network_operations (LXD 5.21)
# finishes a network, ACL or pool write before it answers, and an OVN network create on a
# clustered host can take longer, so a write is sent with a read timeout that outlives it.
WRITE_CONNECT_TIMEOUT_SECONDS = 5


def write_timeout(read_seconds: int) -> tuple:
    """A (connect, read) timeout for one synchronous write, passed per call to pylxd."""
    return (WRITE_CONNECT_TIMEOUT_SECONDS, read_seconds)
_TERMINAL_OPERATION_STATUSES = {"Success", "Done", "Failure", "Cancelled"}


def wait_for_lxd_operation(
    client: pylxd.Client,
    response: object,
    timeout: Optional[int] = None,
    project: Optional[str] = None,
) -> None:
    """Wait for an async LXD operation response to complete.

    A synchronous response (no operation in the body) returns at once, so a write can be
    wrapped unconditionally: LXD 6.9 with the storage_and_network_operations extension
    answers 202 with an operation for network, ACL and storage pool writes that older
    daemons complete before answering.

    `project` names the project the operation was created in when the client is the pooled
    one (no project of its own): an operation on a non-default project's image is not
    visible to a read that does not name that project.
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
    scope: Dict[str, Any] = {"project": project} if project else {}
    if timeout is None:
        wait_response = wait_endpoint.get(params=scope) if scope else wait_endpoint.get()
    else:
        remaining = float(timeout)
        while True:
            slice_seconds = max(1, int(min(OPERATION_WAIT_SLICE_SECONDS, remaining)))
            try:
                wait_response = wait_endpoint.get(params={"timeout": slice_seconds, **scope})
            except pylxd_exceptions.LXDAPIException as exc:
                # important: LXD answers an elapsed wait timeout with an error body, "context
                # deadline exceeded", shaped like a failed request, while the operation keeps
                # running. Read the operation itself before believing the error: a device write
                # on a clustered OVN instance can take longer than one slice.
                wait_response = _operation_record(operation, exc, scope)
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


def _operation_record(
    operation: object, wait_error: Exception, scope: Optional[Dict[str, Any]] = None
) -> object:
    """The operation as LXD holds it now, or the wait's own error if it cannot be read."""
    try:
        return operation.get(params=scope) if scope else operation.get()
    except pylxd_exceptions.LXDAPIException:
        raise wait_error


class _SyntheticResponse:
    """An LXD-shaped error body for a local deadline, so callers see one exception type."""

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
    """An LXD instance bound to the project it was found in, with pylxd-like operations."""
    
    def __init__(self, data, client, project):
        self._data = data
        self._client = client
        self._project = project
        # important: the project the instance was found in, under the name the VMInfo
        # conversion reads. Without it every wrapper-backed read (the single instance GET,
        # start, stop, restart) would report `default`, and the detail dialog would send its
        # device writes to the wrong project.
        self.project = project
        self.name = data.get('name', '')
        self.status = data.get('status', 'Unknown')
        self.status_code = data.get('status_code', 0)
        self.architecture = data.get('architecture', '')
        self.created_at = data.get('created_at')
        self.last_used_at = data.get('last_used_at')
        self.description = data.get('description', '')
        self.type = data.get('type', 'container')
        self.config = data.get('config', {})
        self.expanded_config = data.get('expanded_config')
        # Carried so a root disk size chosen at create time reads back. The LXD instance
        # payload always includes both; a missing key means an older LXD.
        self.devices = data.get('devices', {})
        self.expanded_devices = data.get('expanded_devices', {})
        self.profiles = data.get('profiles', [])
        # Every single-instance read builds its VMInfo from this wrapper, so without `location`
        # the cluster member would read None on every detail while the list payload has it.
        self.location = data.get('location')
    
    def start(self, wait=True):
        response = self._client.api.instances[safe_segment(self.name)].state.put(
            json={'action': 'start'},
            params={'project': self._project}
        )
        if wait:
            wait_for_lxd_operation(self._client, response)
    
    def stop(self, force=False, wait=True, timeout=30):
        response = self._client.api.instances[safe_segment(self.name)].state.put(
            json={'action': 'stop', 'force': force, 'timeout': timeout},
            params={'project': self._project}
        )
        if wait:
            wait_for_lxd_operation(self._client, response, timeout=timeout)
    
    def restart(self, force=False, wait=True, timeout=30):
        response = self._client.api.instances[safe_segment(self.name)].state.put(
            json={'action': 'restart', 'force': force, 'timeout': timeout},
            params={'project': self._project}
        )
        if wait:
            wait_for_lxd_operation(self._client, response, timeout=timeout)
    
    def delete(self, wait=True):
        response = self._client.api.instances[safe_segment(self.name)].delete(params={'project': self._project})
        if wait:
            wait_for_lxd_operation(self._client, response)
    
    def sync(self):
        """Re-read the instance from LXD and refresh its status fields."""
        response = self._client.api.instances[safe_segment(self.name)].get(params={'project': self._project})
        self._data = response.json()['metadata']
        self.status = self._data.get('status', 'Unknown')
        self.status_code = self._data.get('status_code', 0)
    
    def state(self):
        """The instance's state: CPU, memory, disk, network and processes."""
        response = self._client.api.instances[safe_segment(self.name)].state.get(params={'project': self._project})
        state_data = response.json()['metadata']
        
        return StateWrapper(state_data)


class StateWrapper:
    """Instance state data with pylxd-like attributes."""
    
    def __init__(self, data):
        # Remember which keys LXD sent: resource and address consumers expect the `{}` and `0`
        # defaults below, while metrics must tell an omitted field from a real empty or zero value.
        self._present_fields = frozenset(data)
        self.cpu = data.get('cpu', {})
        self.memory = data.get('memory', {})
        self.disk = data.get('disk', {})
        self.network = data.get('network', {})
        self.processes = data.get('processes', 0)
        self.pid = data.get('pid', 0)
        self.status = data.get('status', 'Unknown')
        self.started_at = data.get('started_at')

    def has_field(self, name: str) -> bool:
        """Return whether the raw LXD state payload carried `name`."""
        return name in self._present_fields


def jsonable_lxd(value: Any) -> Any:
    """Recursively convert a pylxd model into something FastAPI can serialise.

    pylxd returns attribute objects (``model.AttributeDict``) and slot-based models (ex;
    `network.state()` returns `pylxd.models.network.NetworkState`). Pydantic's serialiser
    recognises neither, and the failure happens after the handler returns, while the response is
    rendered, so the handler's own try/except never sees it: the caller gets a bare 500 and the
    log a `PydanticSerializationError`.
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
