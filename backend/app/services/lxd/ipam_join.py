"""
Join LXD's network allocations with the instances and networks they belong to.

`/1.0/network-allocations` answers an address, its type and who uses it, and nothing else:
not whether the instance asked for that address or was handed it, not what kind of network
it sits on, not the subnet. Reconciling the list against a corporate IPAM needs all three.
The pin state is decided the way the instances page decides it (a NIC device carrying
`ipv4.address` or `ipv6.address`), read from expanded devices so a profile-inherited pin
counts, and compared as parsed addresses so a prefix or IPv6 compression cannot hide a match.

Pure functions: the router does the reads and hands the dicts in.
"""

import ipaddress
from typing import Any, Dict, List, Optional, Set, Tuple

from app.services.lxd.nic_preflight import managed_network_of
from app.services.lxd.project_scope import DEFAULT_PROJECT, InstanceRecord, effective_project

AllocationKind = str  # static | dhcp | gateway | forward | load_balancer | unknown

_ROW_KINDS: Dict[str, AllocationKind] = {
    "network": "gateway",
    "network-forward": "forward",
    "network-load-balancer": "load_balancer",
}


def _parse(address: str) -> Optional[ipaddress._BaseAddress]:
    try:
        return ipaddress.ip_interface(address.strip()).ip
    except ValueError:
        return None


def nic_candidates(record: InstanceRecord, network: str) -> List[Dict[str, Any]]:
    """The NICs of an instance on `network`; every NIC when none names it (a `parent=` shape)."""
    nics = [
        dev for dev in (record.expanded_devices or {}).values()
        if isinstance(dev, dict) and dev.get("type") == "nic"
    ]
    named = [dev for dev in nics if managed_network_of(dev) == network]
    return named or nics


def pinned_addresses(record: InstanceRecord, network: str, family: int) -> Set[Any]:
    """Every fixed address the instance's NICs on `network` carry for one address family."""
    key = "ipv4.address" if family == 4 else "ipv6.address"
    pinned: Set[Any] = set()
    for dev in nic_candidates(record, network):
        parsed = _parse(str(dev.get(key) or ""))
        if parsed is not None and parsed.version == family:
            pinned.add(parsed)
    return pinned


def classify(
    row_type: str,
    address: str,
    record: Optional[InstanceRecord],
    network: str,
    instances_ok: bool,
) -> AllocationKind:
    """What kind of allocation one LXD row is.

    important: "unknown" is the answer when the instance could not be read, never "dhcp".
    A reconciliation that lists a reservation as a lease is worse than one that says it
    does not know.
    """
    if row_type in _ROW_KINDS:
        return _ROW_KINDS[row_type]
    if row_type != "instance":
        return "unknown"
    if not instances_ok or record is None:
        return "unknown"
    parsed = _parse(address)
    if parsed is None:
        return "unknown"
    return "static" if parsed in pinned_addresses(record, network, parsed.version) else "dhcp"


def _cidr(row: Dict[str, Any], key: str) -> str:
    raw = str((row.get("config") or {}).get(key) or "")
    return "" if raw in ("", "none", "auto") else raw


def network_facts(
    network: str,
    project: str,
    configs: Dict[str, Dict[str, str]],
    networks: Dict[Tuple[str, str], Dict[str, Any]],
) -> Tuple[str, str, str, str]:
    """(type, owning project, ipv4 subnet, ipv6 subnet) of the network a row sits on.

    A project holds its own networks only with `features.networks`; otherwise its rows sit on
    default's networks, which is where the record is looked up. A network that is gone
    between the two reads answers empty facts, which the page renders as its empty glyph.
    """
    owner = effective_project("networks", project, configs)
    row = networks.get((owner, network)) or networks.get((DEFAULT_PROJECT, network))
    if row is None:
        return "", "", "", ""
    # the project the record actually came from, not the one the flags predicted: when the
    # project read failed the prediction is a guess and the default fallback is what matched
    holder = str(row.get("project") or owner)
    return str(row.get("type") or ""), holder, _cidr(row, "ipv4.address"), _cidr(row, "ipv6.address")
