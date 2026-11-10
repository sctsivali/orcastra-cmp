"""How a project relocation recreates the source project's profiles on the target.

A profile is copied with its description, config and devices. A NIC's network (or host parent)
and the root disk's storage pool name things outside the project that may not exist on the
target. They follow the instance contract's rules: an explicit mapping, else the same name when
the target project can use it, else a finding. Host-bound devices and raw daemon config are
refused as they are on an instance.

Everything here is pure. The planner and the worker clone from the same source records, so the
profile a worker creates is the one the plan approved.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional

from .preflight_source import ALLOWED_DEVICES, HARDWARE_DEVICES
from .security import canonical_json_hash
from .target_contract import NicDecision, apply_nic_target, nic_key

Finding = tuple[str, str, str]


@dataclass
class ProfileClone:
    name: str
    description: str
    config: dict[str, Any]
    devices: dict[str, dict[str, Any]]
    root_pool: Optional[str] = None
    nic_decisions: list[NicDecision] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    def record(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "config": dict(self.config),
            "devices": {name: dict(device) for name, device in self.devices.items()},
        }

    @property
    def fingerprint(self) -> str:
        return profile_fingerprint(self.record())

    def summary(self) -> dict[str, Any]:
        """Value-free projection safe for plans, jobs, and the browser."""
        return {
            "fingerprint": self.fingerprint,
            "root_pool": self.root_pool,
            "nic_decisions": [decision.as_dict() for decision in self.nic_decisions],
        }


def profile_fingerprint(profile: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Hash of what a profile applies to its instances. The description is cosmetic."""
    if profile is None:
        return None
    return canonical_json_hash(
        {
            "config": dict(profile.get("config") or {}),
            "devices": {
                str(name): dict(device)
                for name, device in (profile.get("devices") or {}).items()
                if isinstance(device, Mapping)
            },
        }
    )


def _device_findings(profile: str, devices: Mapping[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    for device_name, raw in devices.items():
        if not isinstance(raw, Mapping):
            findings.append(
                (
                    "invalid_profile_device",
                    f"Profile '{profile}' has an unreadable device '{device_name}'.",
                    "Repair the profile and run preflight again.",
                )
            )
            continue
        device_type = str(raw.get("type") or "")
        if device_type in HARDWARE_DEVICES or device_type == "proxy":
            findings.append(
                (
                    "unsupported_profile_device",
                    f"Profile '{profile}' attaches host-bound device '{device_name}' "
                    f"({device_type}).",
                    "Remove the device from the profile, relocate, then add it back on the target.",
                )
            )
        elif device_type not in ALLOWED_DEVICES:
            findings.append(
                (
                    "unknown_or_unsupported_device",
                    f"Profile '{profile}' uses unapproved device type "
                    f"'{device_type or 'unknown'}'.",
                    "Remove it from the profile before relocating the project.",
                )
            )
        elif device_type == "disk" and raw.get("path") != "/" and raw.get("source"):
            findings.append(
                (
                    "attached_volume_or_host_path",
                    f"Profile '{profile}' attaches disk '{device_name}' (custom volume or host "
                    "path).",
                    "Remove the disk from the profile before relocating the project.",
                )
            )
    return findings


def clone_profile(
    profile: Mapping[str, Any],
    *,
    network_mapping: Mapping[str, str],
    usable_networks: Sequence[str],
    pools: Sequence[str],
    root_pool_mapping: Optional[str],
) -> ProfileClone:
    """Decide the target copy of one source profile."""
    name = str(profile.get("name") or "")
    config = {str(key): value for key, value in (profile.get("config") or {}).items()}
    source_devices = {
        str(device_name): dict(device)
        for device_name, device in (profile.get("devices") or {}).items()
        if isinstance(device, Mapping)
    }
    findings = _device_findings(name, profile.get("devices") or {})
    if any(key.startswith("raw.") for key in config):
        findings.append(
            (
                "profile_raw_config",
                f"Profile '{name}' has raw daemon configuration tied to its current host.",
                "Remove the raw.* keys from the profile before relocating the project.",
            )
        )
    usable = set(usable_networks)
    devices: dict[str, dict[str, Any]] = {}
    decisions: list[NicDecision] = []
    root_pool: Optional[str] = None
    for device_name, device in source_devices.items():
        if device.get("type") == "nic":
            key = nic_key(device)
            mapped = network_mapping.get(key) if key else None
            if key is None:
                devices[device_name] = device
                decisions.append(NicDecision(device_name, "none", None, "unattached"))
            elif mapped and mapped in usable:
                devices[device_name] = apply_nic_target(device, mapped)
                decisions.append(NicDecision(device_name, key, mapped, "mapping"))
            elif mapped:
                devices[device_name] = device
                decisions.append(NicDecision(device_name, key, None, "unmapped"))
                findings.append(
                    (
                        "network_mapping_invalid",
                        f"Profile '{name}' NIC '{device_name}' is mapped to '{mapped}', which the "
                        "new target project cannot use.",
                        "Map the network to one of the available target networks.",
                    )
                )
            elif key in usable:
                devices[device_name] = device
                decisions.append(NicDecision(device_name, key, key, "same_name"))
            else:
                devices[device_name] = device
                decisions.append(NicDecision(device_name, key, None, "unmapped"))
                findings.append(
                    (
                        "network_mapping_required",
                        f"Profile '{name}' NIC '{device_name}' uses '{key}', which the new target "
                        "project cannot use.",
                        "Map the network to one of the available target networks.",
                    )
                )
            continue
        if device.get("type") == "disk" and device.get("path") == "/":
            source_pool = device.get("pool")
            chosen = root_pool_mapping or (
                source_pool if isinstance(source_pool, str) and source_pool in pools else None
            )
            if chosen is None:
                findings.append(
                    (
                        "target_pool_mapping_required",
                        f"Profile '{name}' puts root disks on pool '{source_pool}', which the "
                        "target does not have.",
                        "Pick a destination storage pool.",
                    )
                )
                devices[device_name] = device
            elif chosen not in pools:
                findings.append(
                    (
                        "target_pool_missing",
                        f"Target storage pool '{chosen}' does not exist.",
                        "Pick a destination storage pool that exists on the target.",
                    )
                )
                devices[device_name] = device
            else:
                devices[device_name] = {**device, "pool": chosen}
                root_pool = chosen
            continue
        devices[device_name] = device
    return ProfileClone(
        name=name,
        description=str(profile.get("description") or ""),
        config=config,
        devices=devices,
        root_pool=root_pool,
        nic_decisions=decisions,
        findings=findings,
    )


def clone_profiles(
    profiles: Sequence[Mapping[str, Any]],
    *,
    mappings: Mapping[str, Any],
    usable_networks: Sequence[str],
    pools: Sequence[str],
) -> list[ProfileClone]:
    """Clone every source profile, ``default`` first so it is updated before any create."""
    storage = dict((mappings or {}).get("storage_pools") or {})
    root_mapping = storage.get("root") or storage.get("*")
    network_mapping = dict((mappings or {}).get("networks") or {})
    clones = [
        clone_profile(
            profile,
            network_mapping=network_mapping,
            usable_networks=usable_networks,
            pools=pools,
            root_pool_mapping=str(root_mapping) if root_mapping else None,
        )
        for profile in profiles
        if isinstance(profile, Mapping) and profile.get("name")
    ]
    return sorted(clones, key=lambda clone: (clone.name != "default", clone.name))


__all__ = ["ProfileClone", "clone_profile", "clone_profiles", "profile_fingerprint"]
