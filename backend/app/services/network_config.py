"""Supported managed-network configuration at the API boundary."""

import ipaddress
import re
from typing import Dict, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


NetworkType = Literal["bridge", "ovn"]
COMMON_CONFIG_KEYS = frozenset({
    "ipv4.address", "ipv6.address", "ipv4.nat", "ipv6.nat",
    "ipv4.dhcp", "ipv6.dhcp", "ipv4.dhcp.ranges", "ipv6.dhcp.stateful",
    "dns.domain", "security.acls",
})
CONFIG_KEYS_BY_TYPE = {
    "bridge": COMMON_CONFIG_KEYS | {"bridge.mtu", "dns.mode", "ipv6.dhcp.ranges"},
    "ovn": COMMON_CONFIG_KEYS | {"network"},
}
LEGACY_CONFIG_KEYS = frozenset({
    "ipv4.address", "ipv4.nat", "ipv6.address", "ipv6.nat", "network", "dns.domain",
})
# important: raw.dnsmasq, external interfaces, tunnels and arbitrary daemon keys remain
# outside the control plane's supported writes.
ALLOWED_NETWORK_CONFIG_KEYS = frozenset().union(*CONFIG_KEYS_BY_TYPE.values())
BOOLEAN_CONFIG_KEYS = frozenset({
    "ipv4.nat", "ipv6.nat", "ipv4.dhcp", "ipv6.dhcp", "ipv6.dhcp.stateful",
})
NAME_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9-]*$")


def _address(value: str, version: int):
    if value in {"auto", "none"}:
        return None
    if "/" not in value or "%" in value:
        raise ValueError(f"IPv{version} address must be auto, none or a valid CIDR")
    try:
        address = ipaddress.ip_interface(value)
    except ValueError:
        raise ValueError(f"IPv{version} address must be auto, none or a valid CIDR") from None
    if address.version != version:
        raise ValueError(f"IPv{version} address must use IPv{version} CIDR notation")
    return address


def _validate_ranges(value: str, version: int, subnet) -> None:
    parts = value.split(",")
    if len(parts) > 64:
        raise ValueError("At most 64 DHCP ranges are supported")
    ranges = []
    for part in parts:
        endpoints = part.strip().split("-")
        if len(endpoints) != 2:
            raise ValueError(f"IPv{version} DHCP ranges must use FIRST-LAST format")
        try:
            first, last = (ipaddress.ip_address(item.strip()) for item in endpoints)
        except ValueError:
            raise ValueError(f"IPv{version} DHCP ranges must contain valid addresses") from None
        if first.version != version or last.version != version or "%" in part:
            raise ValueError(f"IPv{version} DHCP ranges must contain IPv{version} addresses")
        if first > last:
            raise ValueError("DHCP range start must not exceed its end")
        if subnet is not None:
            network = subnet.network
            if first not in network or last not in network:
                raise ValueError(f"IPv{version} DHCP ranges must be inside the network subnet")
            if first <= subnet.ip <= last:
                raise ValueError("DHCP ranges must not include the network gateway address")
            if version == 4 and network.prefixlen < 31 and (
                first == network.network_address or last == network.broadcast_address
            ):
                raise ValueError("IPv4 DHCP ranges must exclude network and broadcast addresses")
        ranges.append((int(first), int(last)))
    ranges.sort()
    if any(current[0] <= previous[1] for previous, current in zip(ranges, ranges[1:])):
        raise ValueError("DHCP ranges must not overlap")


def validate_network_config(
    config: Dict[str, str],
    network_type: Optional[str] = None,
    *,
    allow_unset: bool = False,
) -> Dict[str, str]:
    """Check safe keys, type-specific values and dependent configuration together."""
    allowed = ALLOWED_NETWORK_CONFIG_KEYS if network_type is None else CONFIG_KEYS_BY_TYPE.get(network_type, LEGACY_CONFIG_KEYS)
    unsupported = sorted(set(config) - allowed)
    if unsupported:
        raise ValueError("Unsupported network config key(s): " + ", ".join(unsupported))
    normalized = {}
    for key, raw in config.items():
        if len(raw) > 4096:
            raise ValueError(f"{key} is too long")
        value = raw.strip()
        if not value and allow_unset:
            normalized[key] = ""
            continue
        if not value:
            raise ValueError(f"Omit {key} to use the hypervisor default")
        if key in BOOLEAN_CONFIG_KEYS and value not in {"true", "false"}:
            raise ValueError(f"{key} must be true or false")
        if key == "bridge.mtu" and (not value.isascii() or not value.isdecimal()
                                    or not 68 <= int(value) <= 65535):
            raise ValueError("Bridge MTU must be an integer between 68 and 65535")
        if key == "dns.mode" and value not in {"managed", "dynamic", "none"}:
            raise ValueError("DNS mode must be managed, dynamic or none")
        if key == "dns.domain":
            domain = value.removesuffix(".")
            labels = domain.split(".")
            if len(domain) > 253 or any(
                not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
                for label in labels
            ):
                raise ValueError("DNS domain must contain valid domain labels")
        if key in {"security.acls", "network"}:
            names = [item.strip() for item in value.split(",")]
            pattern = NAME_PATTERN if key == "security.acls" else re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
            if (key == "network" and len(names) != 1) or len(names) > 64 or any(
                len(name) > 63 or not pattern.fullmatch(name) for name in names
            ):
                raise ValueError(f"{key} must contain valid network or ACL names")
            if len(set(names)) != len(names):
                raise ValueError("ACL names must not repeat")
            value = ",".join(names)
        normalized[key] = value

    # Empty values on an update remove an override rather than inventing a value.
    active = {key: value for key, value in normalized.items() if value}
    for version in (4, 6):
        family = f"ipv{version}"
        address = active.get(f"{family}.address", "auto")
        subnet = _address(address, version)
        if address == "none" and any(key.startswith(f"{family}.")
                                      and key != f"{family}.address" for key in active):
            raise ValueError(f"IPv{version} must be enabled before configuring NAT or DHCP")
        ranges = active.get(f"{family}.dhcp.ranges")
        if active.get(f"{family}.dhcp") == "false" and (
            ranges or (version == 6 and active.get("ipv6.dhcp.stateful") == "true")
        ):
            raise ValueError(f"IPv{version} DHCP must be enabled for ranges or stateful allocation")
        if ranges:
            if version == 6 and active.get("ipv6.dhcp.stateful") != "true":
                raise ValueError("IPv6 DHCP ranges require stateful DHCP")
            if subnet is None:
                raise ValueError(f"IPv{version} DHCP ranges require a custom CIDR address")
            _validate_ranges(ranges, version, subnet)
    mtu = active.get("bridge.mtu")
    if mtu and int(mtu) < 1280 and active.get("ipv6.address", "auto") != "none":
        raise ValueError("Bridge MTU must be at least 1280 when IPv6 is enabled")
    return normalized


class NetworkCreateRequest(BaseModel):
    """Typed, bounded managed-network create contract."""

    name: str = Field(..., min_length=1, max_length=63)
    description: str = Field("", max_length=1024)
    type: NetworkType = "bridge"
    config: Dict[str, str] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not NAME_PATTERN.fullmatch(value):
            raise ValueError("Name must start with a letter and contain only letters, numbers and hyphens")
        return value

    @model_validator(mode="after")
    def _validate_config(self):
        if self.type == "bridge" and len(self.name) > 15:
            raise ValueError("Bridge network names must be at most 15 characters")
        self.config = validate_network_config(self.config, self.type)
        return self
