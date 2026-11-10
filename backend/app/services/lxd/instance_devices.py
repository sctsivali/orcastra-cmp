"""Changing one device on an existing instance, such as a NIC's fixed IPv4.

The device map is read, modified and written back whole instead of PATCHed: a PATCH merges, so
it can set a key but not remove one, and clearing a fixed address back to DHCP needs a removal.
LXD's PUT replaces config, devices and profiles together, so the current object is read first
and only the one device changes.

important: a NIC an instance inherits from a profile does not appear in the instance's own
`devices`. Pinning its address writes the whole expanded device into the instance, as
`lxc config device override` does and as the console does at create time.
"""

import logging
from typing import Any, Dict, Optional, Tuple
from app.core.lxd_path import safe_segment
from app.services.lxd.nic_preflight import managed_network_of
from app.services.lxd.utils import wait_for_lxd_operation

logger = logging.getLogger(__name__)


class DeviceNotFound(LookupError):
    """The named device is not on this instance, by its own devices or a profile's."""


def resolve_device(
    instance_data: Dict[str, Any], device_name: str
) -> Tuple[Dict[str, str], bool]:
    """The device as the instance sees it, and whether it is inherited.

    Returns (device, inherited). An inherited device is read from `expanded_devices`, which is
    the merged view LXD computes from the instance's profiles.
    """
    own = (instance_data.get("devices") or {}).get(device_name)
    if isinstance(own, dict):
        return dict(own), False
    expanded = (instance_data.get("expanded_devices") or {}).get(device_name)
    if isinstance(expanded, dict):
        return dict(expanded), True
    raise DeviceNotFound(device_name)


def nic_network_of(device: Dict[str, str]) -> Optional[str]:
    """The managed network a NIC names, or None when it names a host interface.

    Uses the create-path rule so the two entry points cannot drift. A macvlan NIC on a physical
    parent has no managed network and LXD has no fixed-address key for it, so the caller
    reports that instead of looking for a subnet.
    """
    return managed_network_of(device)


def apply_fixed_address(
    device: Dict[str, str], address: Optional[str]
) -> Dict[str, str]:
    """The device with its fixed IPv4 set, or removed when the address is empty."""
    updated = dict(device)
    trimmed = (address or "").strip()
    if trimmed:
        updated["ipv4.address"] = trimmed
    else:
        updated.pop("ipv4.address", None)
    return updated


def set_nic_address_sync(
    client: Any,
    name: str,
    project: str,
    device_name: str,
    address: Optional[str],
) -> Dict[str, Any]:
    """Set or clear a NIC's fixed IPv4 on an existing instance. Synchronous.

    Returns the device as written. Raises DeviceNotFound when the name is not a device on this
    instance, and ValueError when it is not a NIC.
    """
    params = {"project": project} if project and project != "default" else {}
    response = client.api.instances[safe_segment(name)].get(params=params)
    current = response.json().get("metadata") or {}
    # LXD hands out an ETag on an instance read and honours If-Match on the write. Without it
    # a concurrent edit to config or profiles between this read and the PUT below is
    # overwritten silently, because the PUT replaces the whole object.
    etag = response.headers.get("ETag") if hasattr(response, "headers") else None

    device, inherited = resolve_device(current, device_name)
    if device.get("type") != "nic":
        raise ValueError(
            f"Device '{device_name}' is a {device.get('type') or 'device'}, not a network "
            "interface."
        )

    updated = apply_fixed_address(device, address)
    # important: writing an inherited device onto the instance is `lxc config device override`,
    # and nothing here hands it back to its profile. A Save that changes nothing would detach
    # it, and later edits to the profile's NIC would silently stop reaching this instance.
    if inherited and updated == device:
        logger.info(
            "No change to %s on %s/%s, leaving it inherited", device_name, project, name
        )
        return updated

    devices = dict(current.get("devices") or {})
    devices[device_name] = updated

    payload = {
        "architecture": current.get("architecture"),
        "config": current.get("config") or {},
        "devices": devices,
        "profiles": current.get("profiles") or [],
        "description": current.get("description") or "",
        "ephemeral": bool(current.get("ephemeral")),
        "stateful": bool(current.get("stateful")),
    }
    # LXD rejects a PUT carrying keys it considers read-only, and architecture is absent on
    # some records rather than empty.
    if not payload["architecture"]:
        payload.pop("architecture")

    put_kwargs = {"json": payload, "params": params}
    if etag:
        put_kwargs["headers"] = {"If-Match": etag}
    written = client.api.instances[safe_segment(name)].put(**put_kwargs)
    # important: an instance PUT is an async LXD operation that answers 202 and does the work
    # afterwards. Wait for it, or a refusal (a DHCP-less network, an address already leased, a
    # device LXD rejects) is reported as success.
    wait_for_lxd_operation(client, written, timeout=30)
    if inherited:
        logger.info(
            "Overrode profile-inherited device %s on %s/%s", device_name, project, name
        )
    return devices[device_name]
