"""The guest's SSH server, as read: installed, running, its effective global settings, its host keys.

Read only by design. The values come from `sshd -G` (or `-T` where -G is missing), never from
parsing config files, because
first-match-wins across `sshd_config.d/` (Ubuntu's 50-cloud-init.conf before 60-cloudimg-settings
.conf) makes the file a person would read the wrong answer. Both print the global values: a
`Match` block can still change them for a user or address, and the panel says so. When neither can
run the answer is "unknown", never a guess.

Addresses come from LXD's view of the instance and carry a label. Nothing here tests whether a
person's own machine can reach the guest: a private address works only inside that network.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
from typing import Any, Dict, List, Optional

from . import ssh_keys

_EFFECTIVE = {
    "port", "passwordauthentication", "permitrootlogin", "pubkeyauthentication",
    "kbdinteractiveauthentication", "authorizedkeysfile", "authorizedkeyscommand", "usepam",
}
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _host_key(line: str) -> Optional[Dict[str, Any]]:
    tokens = line.split()
    if len(tokens) < 2 or not tokens[0].startswith(("ssh-", "ecdsa-")):
        return None
    try:
        blob = base64.b64decode(tokens[1], validate=True)
    except (binascii.Error, ValueError):
        return None
    if ssh_keys.blob_type(blob) != tokens[0]:
        return None
    return {"type": tokens[0], "bits": ssh_keys.bits_of(tokens[0], blob), "fingerprint": ssh_keys.fingerprint(blob)}


def parse(stdout: str) -> Dict[str, Any]:
    installed = False
    process = openrc = False
    units: List[Dict[str, str]] = []
    effective: Dict[str, Any] = {}
    effective_known = False
    host_keys: List[Dict[str, Any]] = []
    for line in stdout.splitlines()[:400]:
        if line == "installed=yes":
            installed = True
        elif line == "process=yes":
            process = True
        elif line == "openrc=started":
            openrc = True
        elif line.startswith("unit="):
            parts = line[5:].split()
            if len(parts) == 2 and parts[0] in ("ssh.service", "sshd.service", "ssh.socket", "sshd.socket"):
                units.append({"unit": parts[0], "state": parts[1][:32]})
        elif line.startswith("eff="):
            key, _, value = line[4:].partition(" ")
            key = key.lower()
            if key in _EFFECTIVE and len(value) <= 512 and value.isprintable():
                effective_known = True
                if key == "authorizedkeysfile":
                    effective[key] = value.split()[:8]
                else:
                    effective[key] = value.strip()
        elif line.startswith("hostkey="):
            key = _host_key(line[8:])
            if key is not None and len(host_keys) < 8:
                host_keys.append(key)
    running = process or openrc or any(u["state"] == "active" and u["unit"].endswith(".service") for u in units)
    socket_activated = any(u["state"] == "active" and u["unit"].endswith(".socket") for u in units)
    notes: List[str] = []
    if installed and effective_known:
        files = effective.get("authorizedkeysfile") or []
        if not any(f in (".ssh/authorized_keys", "%h/.ssh/authorized_keys") for f in files):
            notes.append("authorized_keys_not_read")
        if effective.get("authorizedkeyscommand", "none").lower() != "none":
            notes.append("authorized_keys_command")
        if effective.get("pubkeyauthentication", "yes").lower() == "no":
            notes.append("pubkey_disabled")
    return {
        "installed": installed,
        "running": running,
        "socket_activated": socket_activated,
        "units": units,
        "effective": effective if effective_known else None,
        "notes": notes,
        "host_keys": host_keys,
    }


def label(address: str) -> str:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return "unknown"
    if ip.is_link_local:
        return "link_local"
    if ip.version == 4 and ip in _CGNAT:
        return "shared"
    if ip.is_private or ip.is_loopback or ip.is_reserved:
        return "private"
    return "public" if ip.is_global else "private"


def addresses(state: Dict[str, Any]) -> List[Dict[str, str]]:
    """Addresses LXD reports for the instance's interfaces, loopback and link scope left out."""
    found: List[Dict[str, str]] = []
    network = state.get("network") or {}
    if not isinstance(network, dict):
        return found
    for interface, details in sorted(network.items()):
        if interface == "lo" or not isinstance(details, dict):
            continue
        for entry in details.get("addresses") or []:
            if not isinstance(entry, dict) or entry.get("scope") in ("link", "local"):
                continue
            address = str(entry.get("address") or "")
            family = "ipv6" if entry.get("family") == "inet6" else "ipv4"
            if address and len(found) < 32:
                found.append({"address": address, "interface": str(interface)[:32], "family": family, "label": label(address)})
    return found
