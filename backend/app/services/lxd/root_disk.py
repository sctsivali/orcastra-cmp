"""The storage pool an instance's root disk lives on.

Exactly one disk device mounted at "/" with a pool, or no answer. Two root disks, or one without
a pool, are not guessed about: the migration checks compare this value before and after a move,
and the disk I/O gate reads the pool's driver from it.
"""

from typing import Any, Mapping, Optional


def root_pool_of(devices: Any) -> Optional[str]:
    """The pool of the single root disk among `devices`, or None."""
    if not isinstance(devices, Mapping):
        return None
    roots = [
        device
        for device in devices.values()
        if isinstance(device, Mapping) and device.get("type") == "disk" and device.get("path") == "/"
    ]
    if len(roots) != 1 or not isinstance(roots[0].get("pool"), str):
        return None
    return str(roots[0].get("pool") or "") or None


def root_pool(instance: Mapping[str, Any]) -> Optional[str]:
    """The root pool of an instance record, from its expanded devices when it has them."""
    return root_pool_of(instance.get("expanded_devices") or instance.get("devices") or {})
