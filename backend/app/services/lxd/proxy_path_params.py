"""The /proxy params that become an LXD URL path segment, refused before any cluster call when they cannot.

services/lxd/proxy_operations.py puts a caller's instance name, operation id, network, storage pool
and project name into the path pylxd requests. requests folds a `.` or `..` segment out of that path,
so `..` as an instance name addressed the instance collection, and for a delete the server root. Each
branch encodes its name with core/lxd_segment.lxd_segment, and this gate refuses a dot or empty name
with 400 before a client is built, which on the real cluster is already a request.

note: a create's name is a path segment too. pylxd reads a new network, project or storage pool
back by name, and an instance model syncs itself by name. A snapshot's name is the path a later read,
rename or delete of it addresses, and LXD accepts `.` and `..` for one (validate.IsURLSegmentSafe),
so a snapshot created with a dot name could not be deleted through any key door afterwards.
"""

from typing import Any, Dict, Tuple

from app.core.lxd_segment import lxd_segment

__all__ = ["PATH_PARAMS", "assert_path_params"]

_INSTANCE_OPERATIONS = (
    "get_instance", "create_instance", "update_instance", "start_instance", "stop_instance", "restart_instance",
    "delete_instance", "list_snapshots", "create_snapshot", "restore_snapshot", "exec_instance", "get_file",
    "put_file", "desktop_probe", "desktop_view", "desktop_input", "desktop_clipboard",
)

PATH_PARAMS: Dict[str, Tuple[Tuple[str, str], ...]] = {
    **{operation: (("name", "Instance name"),) for operation in _INSTANCE_OPERATIONS},
    "create_snapshot": (("name", "Instance name"), ("snapshot_name", "Snapshot name")),
    "delete_snapshot": (("name", "Instance name"), ("snapshot_name", "Snapshot name")),
    "get_operation": (("operation_id", "Operation id"),),
    "create_network": (("name", "Network name"),),
    "delete_network": (("name", "Network name"),),
    "create_storage_pool": (("name", "Storage pool name"),),
    "delete_storage_pool": (("name", "Storage pool name"),),
    "create_project": (("name", "Project name"),),
    "delete_project": (("name", "Project name"),),
}


def assert_path_params(operation: str, params: Dict[str, Any]) -> None:
    """Raise 400 for a name this operation puts into an LXD path that is empty, `.` or `..`."""
    source = (params.get("definition") or params) if operation == "create_storage_pool" else params
    for field, what in PATH_PARAMS.get(operation, ()):
        if isinstance(source, dict) and field in source:
            lxd_segment(source[field], what=what)
