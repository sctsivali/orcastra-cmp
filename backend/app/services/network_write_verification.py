"""Readback evidence required before reporting a managed-network write as complete."""

import ipaddress
from typing import Dict, Optional

from fastapi import HTTPException


def require_network_readback(metadata, name: str, network_type: str, owner: str) -> dict:
    if (not isinstance(metadata, dict) or metadata.get("name") != name
            or metadata.get("type") != network_type or metadata.get("managed") is not True
            or metadata.get("status", "Created") != "Created"
            or metadata.get("project", owner) != owner):
        raise HTTPException(
            status_code=502,
            detail="Hypervisor readback did not confirm the network; re-check its state before retrying",
        )
    metadata.setdefault("project", owner)
    return metadata


def require_network_update_readback(
    metadata, name: str, network_type: str, owner: str,
    config: Optional[Dict[str, str]], description: Optional[str],
) -> dict:
    observed = require_network_readback(metadata, name, network_type, owner)
    actual_config = observed.get("config") or {}
    confirmed = isinstance(actual_config, dict)
    if description is not None and observed.get("description", "") != description:
        confirmed = False
    for key, expected in (config or {}).items():
        if not expected:
            # A removed override is materialized according to daemon-specific defaults.
            continue
        actual = actual_config.get(key) if isinstance(actual_config, dict) else None
        if key in {"ipv4.address", "ipv6.address"} and expected == "auto":
            try:
                address = ipaddress.ip_interface(actual) if isinstance(actual, str) and "/" in actual else None
                matches = address is not None and address.version == (4 if key.startswith("ipv4") else 6)
            except ValueError:
                matches = False
        elif key in {"ipv4.address", "ipv6.address"} and expected == "none":
            matches = actual is None or (isinstance(actual, str) and actual in {"", "none"})
        elif key == "security.acls" and isinstance(actual, str):
            matches = {item.strip() for item in expected.split(",")} == {item.strip() for item in actual.split(",")}
        elif key == "dns.domain" and isinstance(actual, str):
            matches = expected.rstrip(".").lower() == actual.rstrip(".").lower()
        else:
            matches = actual == expected
        confirmed = confirmed and matches
    if not confirmed:
        raise HTTPException(
            status_code=502,
            detail="Hypervisor readback did not confirm the requested network changes; re-check its state before retrying",
        )
    return observed
