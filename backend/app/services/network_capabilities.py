"""Managed-network features advertised by a trusted hypervisor connection."""

from typing import Dict, List

from fastapi import HTTPException
from pydantic import BaseModel, Field

from app.services.network_config import CONFIG_KEYS_BY_TYPE, NetworkType


class NetworkCapabilities(BaseModel):
    known: bool = Field(description="Whether the daemon supplied API extension metadata")
    types: List[NetworkType]
    config_keys: Dict[str, List[str]]


def network_capabilities(client) -> NetworkCapabilities:
    metadata = getattr(client, "host_info", None)
    extensions = metadata.get("api_extensions") if isinstance(metadata, dict) else None
    known = isinstance(extensions, (list, tuple)) and all(isinstance(item, str) for item in extensions)
    advertised = set(extensions or []) if known else set()
    types: List[NetworkType] = ["bridge"]
    if "network_type_ovn" in advertised:
        types.append("ovn")
    keys = {kind: set(CONFIG_KEYS_BY_TYPE[kind]) for kind in types}
    for kind, supported in keys.items():
        if "network_acl" not in advertised or f"network_{kind}_acl" not in advertised:
            supported.discard("security.acls")
        if kind == "ovn":
            if "network_ovn_nat" not in advertised:
                supported -= {"ipv4.nat", "ipv6.nat"}
            if "network_ovn_dhcp" not in advertised:
                supported -= {"ipv4.dhcp", "ipv6.dhcp", "ipv6.dhcp.stateful"}
            # OVN DHCP controls do not imply support for custom allocation ranges.
            if "ovn_dhcp_ranges" not in advertised:
                supported.discard("ipv4.dhcp.ranges")
    return NetworkCapabilities(known=known, types=types, config_keys={
        kind: sorted(supported) for kind, supported in keys.items()
    })


def validate_network_capabilities(client, network_type: str, config: Dict[str, str]) -> None:
    capabilities = network_capabilities(client)
    # Older adapters may omit extension metadata. Preserve daemon validation as the
    # fallback; absence of metadata is different from an advertised capability denial.
    if not capabilities.known:
        return
    if network_type not in capabilities.types:
        raise HTTPException(status_code=400, detail=f"This hypervisor does not advertise {network_type} networks")
    unsupported = sorted(key for key, value in config.items()
                         if value and key not in capabilities.config_keys[network_type])
    if unsupported:
        raise HTTPException(status_code=400, detail="This hypervisor does not advertise config support for: "
                            + ", ".join(unsupported))
