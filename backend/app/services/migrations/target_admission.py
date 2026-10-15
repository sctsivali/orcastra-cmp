"""Daemon reads that decide whether a target can host a migrated instance.

Preflight and the worker both build the target contract through ``read_target_view`` and
``contract_for`` so the definition the worker submits is derived exactly the way the approved
plan was. Reader functions come from ``preflight`` and are looked up at call time, which keeps
the existing test doubles for ``preflight._instance`` and friends effective.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Optional

from app.core.lxd_path import safe_segment
from app.services.lxd.cpu_allocation import parse_cpu_limit
from app.services.lxd.project_usage import parse_lxd_size, read_project_usage

from . import project_policy
from .target_contract import (
    PARENT_PREFIX,
    TargetContract,
    build_contract,
    effective_root_pool,
    merge_profiles,
    root_device_name,
)

_EMPTY_PROFILE: dict[str, Any] = {"config": {}, "devices": {}}


@dataclass
class TargetView:
    """What the target project looks like, or will look like once the job creates it."""

    project_exists: bool
    project_config: dict[str, Any]
    profile_records: dict[str, Optional[dict[str, Any]]]
    network_records: dict[str, dict[str, Any]]
    usable_networks: list[str]
    pools: list[str]
    findings: list[tuple[str, str, str]] = field(default_factory=list)


def _pf() -> Any:
    from . import preflight

    return preflight


def _effective_project(
    target_project_record: Optional[Mapping[str, Any]],
    source_project_record: Optional[Mapping[str, Any]],
    target_project: str,
) -> tuple[bool, dict[str, Any]]:
    if target_project_record is not None:
        return True, dict(target_project_record)
    clone = project_policy.clone_config(source_project_record or {}, job_id=None)
    return False, {"name": target_project, "config": clone}


def read_target_view(
    *,
    target_client: Any,
    target_project: str,
    target_project_record: Optional[Mapping[str, Any]],
    source_project_record: Optional[Mapping[str, Any]],
    profile_names: list[str],
) -> TargetView:
    """Read target profiles, networks, and pools, simulating a project the job will create."""
    pf = _pf()
    exists, project = _effective_project(
        target_project_record, source_project_record, target_project
    )
    isolated_profiles = project_policy.features_enabled(project, "profiles")
    isolated_networks = project_policy.features_enabled(project, "networks")
    profiles: dict[str, Optional[dict[str, Any]]] = {}
    for name in profile_names:
        if exists or not isolated_profiles:
            lookup_project = target_project if exists else "default"
            profiles[name] = pf._profile(target_client, lookup_project, name)
        else:
            # A new project with its own profiles starts with one empty default profile.
            profiles[name] = dict(_EMPTY_PROFILE) if name == "default" else None

    network_project = target_project if exists else "default"
    try:
        networks_node = target_client.api.networks
    except (AttributeError, TypeError) as exc:
        raise pf.InventoryReadError("network") from exc
    rows = pf._get_collection(networks_node, project=network_project, resource="network")
    networks: dict[str, dict[str, Any]] = {}
    usable: list[str] = []
    for row in rows:
        name = row.get("name")
        if not isinstance(name, str) or not name:
            continue
        managed = bool(row.get("managed"))
        if not exists and isolated_networks and managed:
            # A new isolated project cannot see the default project's managed networks.
            continue
        networks[name] = dict(row)
        status = str(row.get("status") or "").lower()
        if managed and status in {"created", "available", ""}:
            usable.append(name)
        if row.get("type") in {"bridge", "physical", "macvlan"} or not managed:
            usable.append(f"{PARENT_PREFIX}{name}")
    pools = pf._storage_pools(target_client)
    return TargetView(
        project_exists=exists,
        project_config=dict(project.get("config") or {}),
        profile_records=profiles,
        network_records=networks,
        usable_networks=sorted(set(usable)),
        pools=pools,
    )


def resolve_root_pool(
    *,
    source: Mapping[str, Any],
    mapped_pool: Optional[str],
    view: TargetView,
    profile_names: list[str],
) -> tuple[Optional[str], str]:
    """Explicit mapping, else the same pool name, else the target profiles' root pool."""
    if mapped_pool:
        return mapped_pool, "mapping"
    source_pool = effective_root_pool(source)
    if source_pool and source_pool in view.pools:
        return source_pool, "same_name"
    _config, devices = merge_profiles([view.profile_records.get(name) for name in profile_names])
    root_name = root_device_name(devices)
    profile_pool = devices.get(root_name, {}).get("pool") if root_name else None
    if isinstance(profile_pool, str) and profile_pool in view.pools:
        return profile_pool, "target_profile"
    return None, "unresolved"


def contract_for(
    *,
    source: Mapping[str, Any],
    view: TargetView,
    mappings: Mapping[str, Any],
    root_pool: Optional[str],
    intent: str,
    config_overrides: Optional[Mapping[str, Any]] = None,
) -> TargetContract:
    return build_contract(
        source=source,
        target_profiles=view.profile_records,
        profile_mapping=dict((mappings or {}).get("profiles") or {}),
        network_mapping=dict((mappings or {}).get("networks") or {}),
        usable_networks=view.usable_networks,
        root_pool=root_pool,
        intent=intent,
        config_overrides=config_overrides,
    )


def _restricted(config: Mapping[str, Any], key: str, default: str) -> str:
    return str(config.get(key) or default).strip().lower()


def restriction_findings(
    *,
    project_config: Mapping[str, Any],
    contract: TargetContract,
    instance_type: str,
    target_member: Optional[str],
) -> list[tuple[str, str, str]]:
    """Mirror the LXD project restrictions that would refuse this instance on create."""
    if str(project_config.get("restricted") or "").strip().lower() != "true":
        return []
    findings: list[tuple[str, str, str]] = []
    config = contract.expected_config
    nic_rule = _restricted(project_config, "restricted.devices.nic", "managed")
    for name, device in contract.expected_devices.items():
        if device.get("type") == "nic":
            if nic_rule == "block" or (nic_rule == "managed" and not device.get("network")):
                findings.append(
                    (
                        "target_project_restricts_nic",
                        f"The target project does not allow NIC '{name}' as configured.",
                        "Map the NIC to a managed network the project allows.",
                    )
                )
        if device.get("type") == "disk" and device.get("path") != "/":
            if _restricted(project_config, "restricted.devices.disk", "managed") == "block":
                findings.append(
                    (
                        "target_project_restricts_disk",
                        f"The target project does not allow disk device '{name}'.",
                        "Choose an unrestricted target project.",
                    )
                )
    if instance_type == "container":
        if str(config.get("security.nesting") or "").lower() == "true" and _restricted(
            project_config, "restricted.containers.nesting", "block"
        ) != "allow":
            findings.append(
                (
                    "target_project_restricts_nesting",
                    "The target project does not allow nested containers.",
                    "Choose a project with restricted.containers.nesting=allow.",
                )
            )
        if str(config.get("security.privileged") or "").lower() == "true" and _restricted(
            project_config, "restricted.containers.privilege", "unprivileged"
        ) != "allow":
            findings.append(
                (
                    "target_project_restricts_privilege",
                    "The target project does not allow privileged containers.",
                    "Choose a project with restricted.containers.privilege=allow.",
                )
            )
    if target_member and _restricted(project_config, "restricted.cluster.target", "block") != "allow":
        findings.append(
            (
                "target_project_restricts_placement",
                "The target project does not allow choosing a cluster member.",
                "Let the cluster place the instance, or allow restricted.cluster.target.",
            )
        )
    return findings


def quota_findings(
    *,
    target_client: Any,
    target_project: str,
    view: TargetView,
    contract: TargetContract,
    instance_type: str,
) -> list[tuple[str, str, str]]:
    """Refuse a placement that the target project's limits would reject."""
    config = view.project_config
    limit_keys = (
        "limits.instances",
        "limits.containers",
        "limits.virtual-machines",
        "limits.cpu",
        "limits.memory",
        "limits.disk",
    )
    if not any(config.get(key) for key in limit_keys):
        return []
    if view.project_exists:
        usage = read_project_usage(target_client, target_project)
        limits, used = usage.limits, usage.used
        complete = usage.complete_fields
    else:
        limits = {
            "cpu": parse_cpu_limit(config.get("limits.cpu"), default=0) or None,
            "memory_bytes": parse_lxd_size(config.get("limits.memory")),
            "instances": _int(config.get("limits.instances")),
            "containers": _int(config.get("limits.containers")),
            "virtual_machines": _int(config.get("limits.virtual-machines")),
            "disk_bytes": parse_lxd_size(config.get("limits.disk")),
        }
        used = {key: 0 for key in limits}
        complete = set(limits)
    expanded = contract.expected_config
    root_name = root_device_name(contract.expected_devices)
    root_size = parse_lxd_size(
        (contract.expected_devices.get(root_name) or {}).get("size") if root_name else None
    )
    needs = {
        "instances": 1,
        "containers": 1 if instance_type == "container" else 0,
        "virtual_machines": 1 if instance_type == "virtual-machine" else 0,
        "cpu": parse_cpu_limit(expanded.get("limits.cpu"), default=0),
        "memory_bytes": parse_lxd_size(expanded.get("limits.memory")) or 0,
        "disk_bytes": root_size or 0,
    }
    findings: list[tuple[str, str, str]] = []
    for key, need in needs.items():
        limit = limits.get(key)
        if limit is None or need == 0:
            continue
        if key not in complete:
            findings.append(
                (
                    "target_quota_unverified",
                    f"Usage of the target project's {key.replace('_', ' ')} limit is unreadable.",
                    "Restore project usage reporting and run preflight again.",
                )
            )
            continue
        if int(used.get(key) or 0) + int(need) > int(limit):
            findings.append(
                (
                    "target_project_quota_exceeded",
                    f"The target project's {key.replace('_', ' ')} limit cannot fit this instance.",
                    "Raise the project limit or free capacity in it, then run preflight again.",
                )
            )
    return findings


def _int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip()) if value not in (None, "") else None
    except ValueError:
        return None


def static_address_findings(
    *, contract: TargetContract, view: TargetView
) -> list[tuple[str, str, str]]:
    """A static NIC address must fall inside the target network's subnet."""
    findings: list[tuple[str, str, str]] = []
    for name, device in contract.expected_devices.items():
        if device.get("type") != "nic":
            continue
        network = view.network_records.get(str(device.get("network") or ""))
        if network is None:
            continue
        network_config = network.get("config") or {}
        for family in ("ipv4", "ipv6"):
            address = device.get(f"{family}.address")
            subnet = network_config.get(f"{family}.address")
            if not address or not subnet or str(subnet).lower() in {"none", "auto"}:
                continue
            try:
                inside = ipaddress.ip_address(str(address)) in ipaddress.ip_network(
                    str(subnet), strict=False
                )
            except ValueError:
                inside = False
            if not inside:
                findings.append(
                    (
                        "static_address_outside_target_network",
                        f"NIC '{name}' keeps static {family} address {address}, which is outside "
                        f"target network '{device.get('network')}'.",
                        "Map the NIC to a network that contains the address, or change the address.",
                    )
                )
    return findings


def member_findings(
    *,
    target_client: Any,
    target_project: str,
    target_member: str,
    root_pool: Optional[str],
    contract: TargetContract,
    architecture: Optional[str],
) -> list[tuple[str, str, str]]:
    """Prove one exact online member can host the copy with its pool and networks."""
    pf = _pf()
    findings: list[tuple[str, str, str]] = []
    try:
        rows = pf._metadata(
            target_client.api.cluster.members.get(params={"recursion": 1}),
            resource="cluster member",
        )
    except Exception:  # noqa: BLE001
        return [
            (
                "target_member_unverified",
                "Cluster members could not be read on the target.",
                "Restore cluster inventory and run preflight again.",
            )
        ]
    matches = [
        row
        for row in rows or []
        if isinstance(row, dict)
        and (row.get("server_name") == target_member or row.get("name") == target_member)
    ]
    if len(matches) != 1:
        return [
            (
                "target_member_unverified",
                f"Target member '{target_member}' was not found uniquely in cluster inventory.",
                "Select an exact online cluster member and run preflight again.",
            )
        ]
    member = matches[0]
    if str(member.get("status") or "").lower() != "online":
        findings.append(
            (
                "target_member_offline",
                f"Target member '{target_member}' is not online.",
                "Restore the member or choose another one.",
            )
        )
    member_arch = member.get("architecture")
    if architecture and member_arch and member_arch != architecture:
        findings.append(
            (
                "target_member_architecture_mismatch",
                f"Target member '{target_member}' cannot run {architecture}.",
                "Choose a member with the same architecture.",
            )
        )
    for name, device in contract.expected_devices.items():
        network = device.get("network") if device.get("type") == "nic" else None
        if not network:
            continue
        try:
            record = pf._metadata(
                target_client.api.networks[safe_segment(str(network))].get(
                    params={"project": target_project, "target": target_member}
                ),
                resource=f"network '{network}'",
            )
        except Exception:  # noqa: BLE001
            record = None
        locations = record.get("locations") if isinstance(record, dict) else None
        if isinstance(locations, list) and locations and target_member not in locations:
            findings.append(
                (
                    "target_member_network_unavailable",
                    f"Network '{network}' for NIC '{name}' is not available on member "
                    f"'{target_member}'.",
                    "Choose another member or map the NIC to a network present there.",
                )
            )
    return findings


__all__ = [
    "TargetView",
    "contract_for",
    "member_findings",
    "quota_findings",
    "read_target_view",
    "resolve_root_pool",
    "restriction_findings",
    "static_address_findings",
]
