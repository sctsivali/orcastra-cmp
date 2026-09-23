"""
Profile reads and writes against LXD, synchronous, for the profile router to run off the loop.

Same shape as instance_devices.py. The single read keeps the daemon's ETag so an update can
carry it back as If-Match; the update is a read-modify-write (LXD's PUT replaces the whole
object, and a caller that sends only a description must not blank the devices); every verb
carries a write timeout and waits for the operation LXD 6.9 answers a profile PUT with (5.21
answers synchronously and the wait is a no-op); the delete reads first so a profile still in
use is refused with the instances named rather than by the daemon's terse text.
"""

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.core.lxd_names import safe_segment
from app.services.lxd.project_scope import project_params
from app.services.lxd.utils import wait_for_lxd_operation, write_timeout

logger = logging.getLogger(__name__)

# a profile PUT on LXD 6.9 is an operation; same budget as the network and ACL writes
PROFILE_WRITE_WAIT_SECONDS = 60


class ProfileInUse(Exception):
    """Instances still use the profile; `names` are the visible ones."""

    def __init__(self, name: str, names: List[str]) -> None:
        self.name = name
        self.names = names
        shown = ", ".join(names[:8]) + (", ..." if len(names) > 8 else "")
        super().__init__(
            f"Profile {name} is still used by {len(names)} instance(s): {shown}. Detach it from "
            "every instance before deleting it."
        )


def etag_of(response: Any) -> Optional[str]:
    """The ETag header of a raw pylxd read, or None when the daemon (or a fake) sent none."""
    headers = getattr(response, "headers", None)
    return headers.get("ETag") if headers else None


def etags_match(sent: str, current: Optional[str]) -> bool:
    """LXD trims the quotes before comparing, so a client that strips them is not refused."""
    return current is None or sent.strip().strip('"') == current.strip().strip('"')


def instance_names(used_by: List[str]) -> List[str]:
    """The instance names in a used_by list, as an operator would name them."""
    names: List[str] = []
    for ref in used_by:
        if "/instances/" in ref:
            names.append(ref.split("/instances/")[-1].split("?")[0])
    return names


def read_profile_sync(client: Any, name: str, owner: str) -> Tuple[Dict[str, Any], Optional[str]]:
    """The profile as the daemon holds it, and the ETag it holds it at."""
    response = client.api["profiles"][safe_segment(name)].get(params=project_params(owner))
    return (response.json().get("metadata") or {}), etag_of(response)


def create_profile_sync(client: Any, owner: str, body: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[str]]:
    """Create the profile and read it back. POST is synchronous on 5.21 and 6.9; the wait is a no-op."""
    response = client.api["profiles"].post(
        json=body, params=project_params(owner), timeout=write_timeout(PROFILE_WRITE_WAIT_SECONDS)
    )
    wait_for_lxd_operation(client, response, timeout=PROFILE_WRITE_WAIT_SECONDS)
    return read_profile_sync(client, body["name"], owner)


def update_profile_sync(
    client: Any,
    name: str,
    owner: str,
    *,
    config: Optional[Dict[str, str]],
    devices: Optional[Dict[str, Dict[str, str]]],
    description: Optional[str],
    if_match: Optional[str],
    guard: Optional[Callable[[Dict[str, Any], Dict[str, Any]], None]] = None,
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Read-modify-write with the precondition, then read back. Raises PreconditionFailed
    when the fresh read already contradicts `if_match`, before anything is written.

    `guard(payload, current)` may raise to refuse the write. It sees the same read the write is
    built on, so what it judges as unchanged cannot have changed before the PUT."""
    current, current_etag = read_profile_sync(client, name, owner)
    # important: compared here as well as by the daemon. The write below is built on this read,
    # so a precondition the read already contradicts must not become a PUT at all, and a daemon
    # that ignored If-Match would still be caught.
    if if_match and not etags_match(if_match, current_etag):
        raise PreconditionFailed(name)
    payload: Dict[str, Any] = {
        "config": current.get("config") or {},
        "devices": current.get("devices") or {},
        "description": current.get("description") or "",
    }
    if config is not None:
        payload["config"] = config
    if devices is not None:
        payload["devices"] = devices
    if description is not None:
        payload["description"] = description
    if guard is not None:
        guard(payload, current)
    put_kwargs: Dict[str, Any] = {
        "json": payload,
        "params": project_params(owner),
        "timeout": write_timeout(PROFILE_WRITE_WAIT_SECONDS),
    }
    if if_match:
        put_kwargs["headers"] = {"If-Match": if_match}
    response = client.api["profiles"][safe_segment(name)].put(**put_kwargs)
    wait_for_lxd_operation(client, response, timeout=PROFILE_WRITE_WAIT_SECONDS)
    return read_profile_sync(client, name, owner)


def delete_profile_sync(client: Any, name: str, owner: str, visible_used_by: List[str]) -> None:
    """Delete, after refusing a profile the visible instances still use."""
    names = instance_names(visible_used_by)
    if names:
        raise ProfileInUse(name, names)
    response = client.api["profiles"][safe_segment(name)].delete(
        params=project_params(owner), timeout=write_timeout(PROFILE_WRITE_WAIT_SECONDS)
    )
    wait_for_lxd_operation(client, response, timeout=PROFILE_WRITE_WAIT_SECONDS)


class PreconditionFailed(Exception):
    """The profile changed since it was read; the write was not attempted."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"Profile {name} changed since it was read")
