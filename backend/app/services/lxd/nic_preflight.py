"""Refusing an instance create whose network the target project cannot reach.

LXD accepts such a create as an async operation and only fails the background job, so the
instance would sit at "Creating..." with no reason shown. This check refuses it at the boundary
with a clear message instead.

Two details the check has to get right:

- The NIC usually is not in the request. The form submits `profiles: ["default"]` and lets the
  project's default profile supply eth0, so profile devices are merged in before checking.
- A NIC can name its network with `parent` instead of `network`; the console reads both when
  it displays inherited NICs.
"""

import ipaddress
import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.services.lxd.project_scope import (
    DEFAULT_PROJECT,
    effective_project,
    list_in_project,
    list_project_configs,
)

logger = logging.getLogger(__name__)


class NicPreflightError(ValueError):
    """A NIC names something the target project cannot use."""


def _nic_devices(devices: Optional[Dict[str, Any]]) -> List[Tuple[str, Dict[str, Any]]]:
    out: List[Tuple[str, Dict[str, Any]]] = []
    for dev_name, dev in (devices or {}).items():
        if isinstance(dev, dict) and dev.get("type") == "nic":
            out.append((dev_name, dev))
    return out


# The nictypes whose `parent` names an interface on the HOST rather than a managed LXD
# network. LXD resolves those itself and they never appear in a project's network list.
HOST_PARENT_NICTYPES = frozenset(
    {"macvlan", "physical", "sriov", "ipvlan", "routed", "bridged"}
)


def managed_network_of(dev: Dict[str, Any]) -> Optional[str]:
    """The managed network a NIC names, or None when it names a host interface instead.

    important: `network` always names a managed LXD network, but `parent` usually does not.
    For a macvlan, physical, sriov, ipvlan, routed or bridged NIC it names an interface on the
    host, which is not in the project's network list. A stock LXD default profile hands out
    `eth0` as macvlan on a physical parent (ex; `{"nictype": "macvlan", "parent": "enp86s0"}`),
    so treating that parent as a missing network would refuse every create that inherits it.
    """
    value = dev.get("network")
    if value:
        return str(value)
    nictype = str(dev.get("nictype") or "").strip().lower()
    if nictype in HOST_PARENT_NICTYPES:
        return None
    parent = dev.get("parent")
    return str(parent) if parent else None


def _network_of(dev: Dict[str, Any]) -> Optional[str]:
    """Alias of managed_network_of for call sites inside this module."""
    return managed_network_of(dev)


def collect_nics(
    devices: Optional[Dict[str, Any]],
    profile_devices: Iterable[Dict[str, Any]],
) -> List[Tuple[str, Dict[str, Any]]]:
    """Every NIC the instance will end up with, request and profiles together.

    A device in the request overrides a profile device of the same name, which is how the
    console replaces an inherited eth0 rather than adding a second one.
    """
    merged = merge_effective_devices(devices, profile_devices)
    return sorted(_nic_devices(merged))


def merge_effective_devices(
    devices: Optional[Dict[str, Any]],
    profile_devices: Iterable[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Apply LXD profile order and instance overrides to the complete device map.

    Later profiles replace earlier ones and the instance wins; ``type=none`` removes the
    inherited device. Device type is filtered only after merging: filtering each profile to NICs
    first would lose a ``type=none`` override and report an isolated instance as networked.
    """
    merged: Dict[str, Dict[str, Any]] = {}
    for profile in profile_devices:
        for dev_name, raw in (profile or {}).items():
            if isinstance(raw, dict):
                merged[str(dev_name)] = dict(raw)

    for dev_name, raw in (devices or {}).items():
        if not isinstance(raw, dict):
            continue
        merged[str(dev_name)] = dict(raw)

    return {
        name: device
        for name, device in merged.items()
        if str(device.get("type") or "").strip().lower() != "none"
    }


def _subnet_of(network_row: Dict[str, Any]) -> Optional[ipaddress.IPv4Network]:
    raw = (network_row.get("config") or {}).get("ipv4.address")
    if not raw or raw in ("none", "auto"):
        return None
    try:
        return ipaddress.ip_interface(raw).network
    except ValueError:
        return None


def validate_nics(
    project: str,
    nics: Iterable[Tuple[str, Dict[str, Any]]],
    network_rows: Iterable[Dict[str, Any]],
) -> None:
    """Validate an already-resolved NIC set against one project's network records."""
    rows = list(network_rows)
    by_name = {row.get("name"): row for row in rows if row.get("name")}
    usable = sorted(name for name, row in by_name.items() if row.get("managed"))

    def _offer() -> str:
        if usable:
            return f" Networks available in project '{project}': {', '.join(usable)}."
        return (
            f" Project '{project}' has no managed network to attach to. Create one, or attach"
            " the instance to a project that has one."
        )

    for dev_name, dev in nics:
        wanted_network = _network_of(dev)
        if not wanted_network:
            continue

        row = by_name.get(wanted_network)
        if row is None:
            raise NicPreflightError(
                f"Interface '{dev_name}' asks for network '{wanted_network}', which does not "
                f"exist in project '{project}'." + _offer()
            )
        if not row.get("managed"):
            raise NicPreflightError(
                f"Interface '{dev_name}' asks for network '{wanted_network}', which LXD does "
                "not manage, so it cannot be attached by name." + _offer()
            )

        address = dev.get("ipv4.address")
        if not address:
            continue
        try:
            parsed = ipaddress.IPv4Address(str(address))
        except ValueError:
            raise NicPreflightError(
                f"Interface '{dev_name}' has an invalid IPv4 address: '{address}'."
            ) from None
        subnet = _subnet_of(row)
        if subnet is None:
            raise NicPreflightError(
                f"Interface '{dev_name}' asks for the fixed address {address}, but network "
                f"'{wanted_network}' has no IPv4 subnet to allocate it from."
            )
        if parsed not in subnet:
            raise NicPreflightError(
                f"Interface '{dev_name}' asks for {address}, which is outside "
                f"{wanted_network}'s subnet {subnet}."
            )
        if parsed == subnet.network_address or parsed == subnet.broadcast_address:
            raise NicPreflightError(
                f"Interface '{dev_name}' asks for {address}, which is the "
                f"{'network' if parsed == subnet.network_address else 'broadcast'} address of "
                f"{subnet}."
            )


def check_nics(
    client: Any,
    project: Optional[str],
    devices: Optional[Dict[str, Any]],
    profiles: Optional[List[str]],
) -> None:
    """Raise NicPreflightError when a NIC cannot work in `project`.

    `client` must be the pooled client. pylxd overwrites an explicit `project` parameter with
    the client's own whenever the client carries one, so a project-bound client here would
    silently check the wrong project.
    """
    project = project or DEFAULT_PROJECT
    configs = list_project_configs(client) if project != DEFAULT_PROJECT else {}

    profile_owner = effective_project("profiles", project, configs)
    profile_devices: List[Dict[str, Any]] = []
    wanted = list(dict.fromkeys(profiles or ["default"]))
    if wanted:
        try:
            rows = list_in_project(client, "profiles", profile_owner)
            by_name = {row.get("name"): row for row in rows if row.get("name")}
            # important: LXD applies profiles in request order rather than daemon listing order.
            # Later profiles can replace a device from an earlier profile.
            profile_devices = [
                (by_name[name].get("devices") or {})
                for name in wanted
                if name in by_name
            ]
        except Exception as exc:  # noqa: BLE001 - a profile read failure is not a NIC fault
            # Do not invent a refusal out of a failed read. Let LXD answer instead.
            logger.warning("Could not read profiles in %s for pre-flight: %s", profile_owner, exc)
            return

    nics = collect_nics(devices, profile_devices)
    if not nics:
        return

    network_owner = effective_project("networks", project, configs)
    try:
        rows = list_in_project(client, "networks", network_owner)
    except Exception as exc:  # noqa: BLE001 - same reasoning as above
        logger.warning("Could not read networks in %s for pre-flight: %s", network_owner, exc)
        return
    validate_nics(project, nics, rows)


def check_address_for_network(
    client: Any,
    project: Optional[str],
    network_name: Optional[str],
    address: str,
) -> None:
    """Validate one fixed IPv4 against the network a NIC is attached to.

    The create path checks a whole device map before an instance exists. This checks a single
    address against a NIC that already does, which is what the device write path needs.
    """
    trimmed = (address or "").strip()
    if not trimmed:
        return
    try:
        parsed = ipaddress.IPv4Address(trimmed)
    except ValueError:
        raise NicPreflightError(f"'{address}' is not a valid IPv4 address.") from None

    if not network_name:
        raise NicPreflightError(
            "This interface is not attached to a managed network, so an address cannot be "
            "reserved for it."
        )

    project = project or DEFAULT_PROJECT
    configs = list_project_configs(client) if project != DEFAULT_PROJECT else {}
    owner = effective_project("networks", project, configs)
    rows = list_in_project(client, "networks", owner)
    row = next((r for r in rows if r.get("name") == network_name), None)
    if row is None:
        raise NicPreflightError(
            f"Network '{network_name}' does not exist in project '{project}'."
        )
    # LXD lists a bare host interface as an unmanaged network, so a NIC whose `parent` is one
    # (a macvlan off a host NIC, as a stock default profile hands out) resolves here. Refuse it
    # as unmanaged, like the create-time check, rather than with a misleading subnet error.
    if not row.get("managed"):
        raise NicPreflightError(
            f"Interface is attached to '{network_name}', which LXD does not manage, so an "
            "address cannot be reserved for it. Attach it to a managed network first."
        )

    subnet = _subnet_of(row)
    if subnet is None:
        raise NicPreflightError(
            f"Network '{network_name}' has no IPv4 subnet to allocate an address from."
        )
    if parsed not in subnet:
        raise NicPreflightError(f"{address} is outside {network_name}'s subnet {subnet}.")
    if parsed == subnet.network_address or parsed == subnet.broadcast_address:
        raise NicPreflightError(
            f"{address} is the "
            f"{'network' if parsed == subnet.network_address else 'broadcast'} address of "
            f"{subnet}."
        )
