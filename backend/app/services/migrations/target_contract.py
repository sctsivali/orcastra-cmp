"""The definition a migrated instance is created with, and the state it must read back as.

A copy or staged move recreates the instance on the target from profile names, local config and
local devices; its effective state there is the target profiles merged with that definition, so
the source's local definition alone does not reproduce it. This module computes the local
definition to send (pinning whatever the target profiles would get wrong: root pool and size, NIC
networks, config values; masking profile devices the source lacked with a ``none`` device), the
effective config and devices the target must read back with, and one contract hash the plan
approves and the worker re-derives before it submits anything.

Everything here is pure. Daemon reads happen in ``target_admission``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional

from .project_policy import MIGRATION_JOB_MARKER
from .security import canonical_json_hash

PARENT_PREFIX = "parent:"
_IGNORED_DEVICE_KEYS = frozenset({"hwaddr"})


@dataclass
class NicDecision:
    device: str
    source: str
    target: Optional[str]
    via: str

    def as_dict(self) -> dict[str, Any]:
        return {"device": self.device, "source": self.source, "target": self.target, "via": self.via}


@dataclass
class TargetContract:
    profiles: list[str]
    config: dict[str, Any]
    devices: dict[str, dict[str, Any]]
    expected_config: dict[str, Any]
    expected_devices: dict[str, dict[str, Any]]
    root_pool: Optional[str]
    nic_decisions: list[NicDecision] = field(default_factory=list)
    pinned_config: list[str] = field(default_factory=list)
    pinned_devices: list[str] = field(default_factory=list)
    unmapped_networks: list[dict[str, Any]] = field(default_factory=list)
    profile_added_config: list[str] = field(default_factory=list)
    missing_profiles: list[str] = field(default_factory=list)

    @property
    def contract_hash(self) -> str:
        return canonical_json_hash(
            {
                "profiles": self.profiles,
                "config": _without_marker(self.config),
                "devices": self.devices,
                "expected_config": self.expected_config,
                "expected_devices": self.expected_devices,
            }
        )

    def definition(
        self,
        *,
        instance_type: str,
        architecture: Optional[str],
        job_id: str,
        preserved: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        config = dict(self.config)
        config.update(dict(preserved or {}))
        config[MIGRATION_JOB_MARKER] = job_id
        return {
            "type": instance_type,
            "architecture": architecture,
            "profiles": list(self.profiles),
            "config": config,
            "devices": {name: dict(device) for name, device in self.devices.items()},
        }

    def summary(self) -> dict[str, Any]:
        """Value-free projection safe for plans, jobs, and the browser."""
        return {
            "contract_hash": self.contract_hash,
            "profiles": list(self.profiles),
            "root_pool": self.root_pool,
            "nic_decisions": [decision.as_dict() for decision in self.nic_decisions],
            "pinned_config": sorted(self.pinned_config),
            "pinned_devices": sorted(self.pinned_devices),
            "profile_added_config": sorted(self.profile_added_config),
        }


def preserved_identity_config(
    source: Mapping[str, Any], contract: "TargetContract", *, intent: str
) -> dict[str, Any]:
    """Generated NIC MAC addresses a move keeps, so DHCP leases and static mappings survive.

    A copy never keeps them: two live instances must not share a MAC address.
    """
    if intent != "move":
        return {}
    config = source.get("config") or {}
    preserved: dict[str, Any] = {}
    for name, device in contract.devices.items():
        if device.get("type") != "nic" or device.get("hwaddr"):
            continue
        key = f"volatile.{name}.hwaddr"
        if isinstance(config.get(key), str) and config[key]:
            preserved[key] = config[key]
    for name in contract.expected_devices:
        key = f"volatile.{name}.hwaddr"
        if name not in contract.devices and isinstance(config.get(key), str) and config[key]:
            preserved[key] = config[key]
    return preserved


def _without_marker(config: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in config.items() if key != MIGRATION_JOB_MARKER}


def _stable_config(config: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in config.items()
        if not str(key).startswith("volatile.") and str(key) != MIGRATION_JOB_MARKER
    }


def _device(device: Any) -> dict[str, Any]:
    return dict(device) if isinstance(device, Mapping) else {}


def _comparable_devices(devices: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Devices as compared on readback: ``none`` masks dropped, generated keys ignored."""
    result: dict[str, dict[str, Any]] = {}
    for name, raw in devices.items():
        device = _device(raw)
        if device.get("type") == "none":
            continue
        result[str(name)] = {
            key: value
            for key, value in device.items()
            if key not in _IGNORED_DEVICE_KEYS and not str(key).startswith("volatile.")
        }
    return result


def merge_profiles(
    profiles: Sequence[Optional[Mapping[str, Any]]],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Merge profile records in order, later profiles overriding, as LXD expands them."""
    config: dict[str, Any] = {}
    devices: dict[str, dict[str, Any]] = {}
    for profile in profiles:
        if not isinstance(profile, Mapping):
            continue
        config.update(dict(profile.get("config") or {}))
        for name, device in (profile.get("devices") or {}).items():
            devices[str(name)] = _device(device)
    return config, devices


def root_device_name(devices: Mapping[str, Any]) -> Optional[str]:
    for name, device in devices.items():
        if isinstance(device, Mapping) and device.get("type") == "disk" and device.get("path") == "/":
            return str(name)
    return None


def nic_key(device: Mapping[str, Any]) -> Optional[str]:
    network = device.get("network")
    if isinstance(network, str) and network:
        return network
    parent = device.get("parent")
    if isinstance(parent, str) and parent:
        return f"{PARENT_PREFIX}{parent}"
    return None


def apply_nic_target(device: Mapping[str, Any], target_key: str) -> dict[str, Any]:
    mapped = dict(device)
    if target_key.startswith(PARENT_PREFIX):
        mapped.pop("network", None)
        mapped["parent"] = target_key[len(PARENT_PREFIX):]
        if "nictype" not in mapped:
            mapped["nictype"] = "bridged"
    else:
        mapped["network"] = target_key
        # A managed network decides nictype and parent itself and refuses explicit values.
        mapped.pop("nictype", None)
        mapped.pop("parent", None)
    return mapped


def build_contract(
    *,
    source: Mapping[str, Any],
    target_profiles: Mapping[str, Optional[Mapping[str, Any]]],
    profile_mapping: Mapping[str, str],
    network_mapping: Mapping[str, str],
    usable_networks: Sequence[str],
    root_pool: Optional[str],
    intent: str,
    config_overrides: Optional[Mapping[str, Any]] = None,
) -> TargetContract:
    """Derive the target definition and its expected effective state from live source state."""
    source_profiles = [str(name) for name in (source.get("profiles") or [])]
    profiles = [str(profile_mapping.get(name, name)) for name in source_profiles]
    missing_profiles = [name for name in profiles if target_profiles.get(name) is None]
    profile_config, profile_devices = merge_profiles(
        [target_profiles.get(name) for name in profiles]
    )

    source_local_config = _stable_config(source.get("config") or {})
    source_effective_config = _stable_config(
        source.get("expanded_config") or source.get("config") or {}
    )
    if config_overrides:
        source_effective_config.update(dict(config_overrides))
        source_local_config.update(
            {key: value for key, value in config_overrides.items() if key in source_local_config}
        )
    source_local_devices = {
        str(name): _device(device) for name, device in (source.get("devices") or {}).items()
    }
    source_effective_devices = {
        str(name): _device(device)
        for name, device in (
            source.get("expanded_devices") or source.get("devices") or {}
        ).items()
    }

    # Desired effective devices: the source's, with NIC networks and the root pool decided.
    usable = set(usable_networks)
    desired_devices: dict[str, dict[str, Any]] = {}
    nic_decisions: list[NicDecision] = []
    unmapped: list[dict[str, Any]] = []
    for name, device in source_effective_devices.items():
        if device.get("type") != "nic":
            desired_devices[name] = device
            continue
        key = nic_key(device)
        if key is None:
            desired_devices[name] = device
            nic_decisions.append(NicDecision(name, "none", None, "unattached"))
            continue
        mapped = network_mapping.get(key)
        inherited = name not in source_local_devices
        target_profile_nic = profile_devices.get(name)
        if mapped and mapped in usable:
            desired_devices[name] = apply_nic_target(device, mapped)
            nic_decisions.append(NicDecision(name, key, mapped, "mapping"))
        elif mapped:
            # important: a mapping may only name a network the target project can use. Anything
            # else could attach the NIC to a network owned by another project.
            desired_devices[name] = device
            unmapped.append({"device": name, "source": key, "mapped": str(mapped)})
            nic_decisions.append(NicDecision(name, key, None, "unmapped"))
        elif inherited and target_profile_nic and target_profile_nic.get("type") == "nic":
            desired_devices[name] = dict(target_profile_nic)
            nic_decisions.append(
                NicDecision(name, key, nic_key(target_profile_nic), "target_profile")
            )
        elif key in usable:
            desired_devices[name] = device
            nic_decisions.append(NicDecision(name, key, key, "same_name"))
        else:
            desired_devices[name] = device
            unmapped.append({"device": name, "source": key})
            nic_decisions.append(NicDecision(name, key, None, "unmapped"))

    root_name = root_device_name(desired_devices)
    if root_name is not None and root_pool:
        root = dict(desired_devices[root_name])
        root["pool"] = root_pool
        desired_devices[root_name] = root

    # Local devices: keep every source-local device (decided), pin anything the target
    # profiles would render differently, and mask profile devices the source never had.
    local_devices: dict[str, dict[str, Any]] = {}
    pinned_devices: list[str] = []
    for name, desired in desired_devices.items():
        if name in source_local_devices:
            local_devices[name] = desired
            continue
        if _comparable_devices({name: profile_devices.get(name) or {}}) != _comparable_devices(
            {name: desired}
        ):
            local_devices[name] = desired
            pinned_devices.append(name)
    for name in profile_devices:
        if name not in desired_devices:
            local_devices[name] = {"type": "none"}
            pinned_devices.append(name)
    if intent == "copy":
        for device in local_devices.values():
            device.pop("hwaddr", None)

    local_config = dict(source_local_config)
    pinned_config: list[str] = []
    for key, value in source_effective_config.items():
        if key in local_config:
            continue
        if profile_config.get(key) != value:
            local_config[key] = value
            pinned_config.append(key)
    profile_added = [key for key in profile_config if key not in source_effective_config]

    expected_config = _stable_config({**profile_config, **local_config})
    expected_devices = _comparable_devices({**profile_devices, **local_devices})
    return TargetContract(
        profiles=profiles,
        config=local_config,
        devices=local_devices,
        expected_config=expected_config,
        expected_devices=expected_devices,
        root_pool=root_pool,
        nic_decisions=nic_decisions,
        pinned_config=pinned_config,
        pinned_devices=pinned_devices,
        unmapped_networks=unmapped,
        profile_added_config=profile_added,
        missing_profiles=missing_profiles,
    )


def mismatches(record: Optional[Mapping[str, Any]], contract: TargetContract) -> list[str]:
    """Readback differences between a target instance and its contract, by field name only."""
    if record is None:
        return ["missing"]
    problems: list[str] = []
    if [str(name) for name in (record.get("profiles") or [])] != contract.profiles:
        problems.append("profiles")
    config = _stable_config(record.get("expanded_config") or record.get("config") or {})
    if config != contract.expected_config:
        problems.extend(
            f"config:{key}"
            for key in sorted(set(config) | set(contract.expected_config))
            if config.get(key) != contract.expected_config.get(key)
        )
    devices = _comparable_devices(record.get("expanded_devices") or record.get("devices") or {})
    if devices != contract.expected_devices:
        problems.extend(
            f"device:{name}"
            for name in sorted(set(devices) | set(contract.expected_devices))
            if devices.get(name) != contract.expected_devices.get(name)
        )
    return problems


def target_matches(record: Optional[Mapping[str, Any]], contract: TargetContract) -> bool:
    return not mismatches(record, contract)


def effective_root_pool(instance: Mapping[str, Any]) -> Optional[str]:
    devices = instance.get("expanded_devices") or instance.get("devices") or {}
    name = root_device_name(devices)
    if name is None:
        return None
    pool = _device(devices.get(name)).get("pool")
    return str(pool) if isinstance(pool, str) and pool else None


__all__ = [
    "NicDecision",
    "PARENT_PREFIX",
    "apply_nic_target",
    "preserved_identity_config",
    "TargetContract",
    "build_contract",
    "effective_root_pool",
    "merge_profiles",
    "mismatches",
    "nic_key",
    "root_device_name",
    "target_matches",
]
