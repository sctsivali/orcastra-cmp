"""A project-limited key's raw proxy call to one LXD operation, bounded by who owns it.

The raw proxy binds a request to a granted project by forcing `?project=`, but LXD ignores
`?project=` on `/1.0/operations/{id}`. Without this check a key granted only `default` that held
another project's operation id could read it (`resources` and `err` included), wait on it, and
with `admin` cancel it. So the operation is read first, and unless its resources name a project
the key holds, the caller gets the answer LXD gives for an id it does not hold, as on the other
key surfaces.
"""

from typing import Any, Dict, FrozenSet, Optional, Tuple

from app.core.lxd_segment import lxd_segment
from app.core.raw_proxy_path import ProxyPath

from .operation_ownership import visible_to
from .proxy import raw_request

__all__ = ["LXD_OPERATION_NOT_FOUND", "OwnerUnknown", "addressed_operation_id", "operation_is_visible"]

# The body LXD answers for an operation id it does not hold, so a refusal is not an ownership
# oracle.
LXD_OPERATION_NOT_FOUND: Dict[str, Any] = {
    "type": "error",
    "status": "",
    "status_code": 0,
    "operation": "",
    "error_code": 404,
    "error": "Operation not found",
    "metadata": None,
}


def addressed_operation_id(path: ProxyPath) -> Optional[str]:
    """The id in `/1.0/operations/{id}` and anything below it, or None for any other path."""
    if len(path.segments) >= 2 and path.lowered[0] == "operations" and path.segments[1]:
        return path.segments[1]
    return None


def lxd_operation_id(body: Any) -> Optional[str]:
    """The operation id of an async (202) LXD body: `operation` is `/1.0/operations/{id}`."""
    if not isinstance(body, dict):
        return None
    path = body.get("operation")
    if isinstance(path, str) and path.strip("/"):
        return path.rstrip("/").split("/")[-1].split("?")[0] or None
    metadata = body.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("id"), str):
        return metadata["id"] or None
    return None


class OwnerUnknown(Exception):
    """LXD answered the ownership read with something other than the operation or a 404."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"LXD answered {status_code} when asked whose operation this is")
        self.status_code = status_code


def operation_is_visible(
    session: Any,
    endpoint: str,
    operation_id: str,
    visible_projects: FrozenSet[str],
    timeout: Tuple[int, int],
) -> bool:
    """Blocking: read the operation and say whether its owner is a project the caller holds.

    False for an id LXD does not hold, so both cases reach the caller as the same 404.
    """
    # `endpoint` is the client's API endpoint, which already ends in /1.0.
    segment = lxd_segment(operation_id, what="operation id")
    response = raw_request(session, "GET", f"{endpoint}/operations/{segment}", None, timeout)
    if response.status_code == 404:
        return False
    if response.status_code != 200:
        raise OwnerUnknown(response.status_code)
    try:
        body = response.json()
    except ValueError:
        raise OwnerUnknown(response.status_code)
    operation = body.get("metadata") if isinstance(body, dict) else None
    return isinstance(operation, dict) and visible_to(operation, visible_projects)
