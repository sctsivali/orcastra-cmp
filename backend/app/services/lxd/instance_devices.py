"""Changing one device on an existing instance.

Chapter 5.1 of the EcoBytes audit asks for a fixed IPv4 that can be set from the console rather
than through `lxc config device set` on the hypervisor. Setting it at create time is half the
answer; an address planned against a corporate IPAM catalogue also has to be changeable on an
instance that already exists, and for an operator without SSH to the host that is the whole
point of the request.

Read, modify, write the whole device map rather than PATCH it. A PATCH merges, so it can set a
key but cannot remove one, and clearing a fixed address back to DHCP is exactly as important as
setting it. LXD's PUT replaces config, devices and profiles together, so the current object is
read first and only the one device is changed.

important: a NIC an instance inherits from a profile does not appear in the instance's own
`devices`. Pinning its address writes the whole expanded device into the instance, which is
what `lxc config device override` does and what the console already does at create time.
"""

import logging
from typing import Any, Dict, Optional, Tuple
from app.core.lxd_names import safe_segment
from app.services.lxd.nic_preflight import managed_network_of

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

    Delegates to the create-path rule so the two entry points cannot drift: a macvlan NIC on a
    physical parent has no managed network, and LXD has no fixed-address key for it, so the
    caller reports that rather than hunting for a subnet the host interface will never have.
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

    devices = dict(current.get("devices") or {})
    devices[device_name] = apply_fixed_address(device, address)

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
    client.api.instances[safe_segment(name)].put(**put_kwargs)
    if inherited:
        logger.info(
            "Overrode profile-inherited device %s on %s/%s", device_name, project, name
        )
    return devices[device_name]
