"""Ownership-aware policy for writes that can reach the LXD host.

Partners normally own their cluster and keep the full LXD surface. A physical cluster may,
however, be bound to more than one organization. On that boundary only project-contained,
typed settings are safe: a raw config key, passthrough device, host path or unmanaged NIC
would let one organization affect or inspect another organization's host resources.

The policy is intentionally independent from the instance router. Profile and project writes
use it here, and instance create/update can call the same validator rather than growing a
second allowlist that eventually drifts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthenticatedUser
from app.core.rbac import Role, get_highest_role, resolve_roles
from app.models.db_models import OrganizationCluster
from app.services import cluster_parties


class ClusterOwnership(str, Enum):
    """How confidently one organization owns a physical cluster."""

    EXCLUSIVE = "exclusive"
    SHARED = "shared"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class ClusterWritePolicy:
    """The write surface available to one actor on one cluster."""

    cluster_id: str
    role: Role
    ownership: ClusterOwnership

    @property
    def requires_safe_allowlist(self) -> bool:
        return self.role == Role.PARTNER and self.ownership != ClusterOwnership.EXCLUSIVE

    @property
    def has_full_access(self) -> bool:
        return self.role == Role.ADMIN or (
            self.role == Role.PARTNER and self.ownership == ClusterOwnership.EXCLUSIVE
        )


@dataclass(frozen=True)
class PolicyFinding:
    """A stable, value-free reason a payload was refused."""

    code: str
    field: str


class InstanceSecurityPolicyViolation(Exception):
    """A sanitized policy refusal suitable for an API response and audit record."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 403,
        findings: Sequence[PolicyFinding] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.findings = tuple(findings)

    def as_detail(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "violations": [
                {"code": finding.code, "field": finding.field}
                for finding in self.findings
            ],
        }


async def resolve_cluster_write_policy(
    user: AuthenticatedUser,
    cluster_id: str,
    db: AsyncSession,
) -> ClusterWritePolicy:
    """Resolve the current ownership shape directly from organization bindings.

    Admin deliberately short-circuits without a database read. For Partner, anything other
    than exactly one bound organization on the partner's own side is restricted. An unbound registrant row is valid for
    cluster visibility, but it is not enough evidence to grant host-level capabilities.
    """

    role = get_highest_role(resolve_roles(user))
    if role == Role.ADMIN:
        return ClusterWritePolicy(cluster_id, role, ClusterOwnership.EXCLUSIVE)
    if role == Role.TENANT:
        return ClusterWritePolicy(cluster_id, role, ClusterOwnership.AMBIGUOUS)

    rows = (
        await db.execute(
            select(
                OrganizationCluster.organization_id,
                OrganizationCluster.registered_by,
            ).where(OrganizationCluster.cluster_id == cluster_id)
        )
    ).all()

    bound_organizations = {int(row[0]) for row in rows if row[0] is not None}
    has_unbound_row = any(row[0] is None for row in rows)
    if has_unbound_row or not bound_organizations:
        ownership = ClusterOwnership.AMBIGUOUS
    elif len(bound_organizations) > 1:
        ownership = ClusterOwnership.SHARED
    elif cluster_id in await cluster_parties.clusters_held_by_another_party(db, user.email, {cluster_id}):
        # important: one binding is not one owner. A partner reaches a cluster another party holds
        # alone through a member row in an organization it bought from, and EXCLUSIVE would hand it
        # host-level config (privileged containers, raw.lxc, host disks) on the seller's host.
        ownership = ClusterOwnership.SHARED
    else:
        ownership = ClusterOwnership.EXCLUSIVE
    return ClusterWritePolicy(cluster_id, role, ownership)


def ensure_write_role(policy: ClusterWritePolicy) -> None:
    if policy.role == Role.TENANT:
        raise InstanceSecurityPolicyViolation(
            "instance_write_not_allowed",
            "This role cannot change instance, profile, or project configuration.",
        )


_SAFE_CONFIG_KEYS = {
    "limits.cpu",
    "limits.memory",
    "limits.processes",
    "limits.disk.priority",
    "limits.network.priority",
}
_SAFE_CONFIG_PREFIXES = ("cloud-init.", "user.")
_CPU_COUNT_RE = re.compile(r"^[1-9][0-9]*$")
_POSITIVE_SIZE_RE = re.compile(
    r"^(?:[1-9][0-9]*)(?:\.[0-9]+)?(?:B|KiB|MiB|GiB|TiB|kB|MB|GB|TB)$"
)
_VOLUME_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

_NIC_KEYS = {
    "type",
    "network",
    "name",
    "hwaddr",
    "ipv4.address",
    "ipv6.address",
    "security.acls",
    "limits.ingress",
    "limits.egress",
    "limits.max",
    "queue.tx.length",
    "boot.priority",
}
_DISK_KEYS = {
    "type",
    "path",
    "pool",
    "source",
    "size",
    "size.state",
    "readonly",
    "recursive",
    "shift",
    "boot.priority",
}
_HOST_DEVICE_TYPES = {
    "gpu",
    "pci",
    "usb",
    "unix-char",
    "unix-block",
    "unix-hotplug",
    "proxy",
    "infiniband",
    "tpm",
}
_UNMANAGED_NIC_TYPES = {"physical", "macvlan", "sriov", "ipvlan", "routed"}


def _validate_config(config: Mapping[str, Any]) -> list[PolicyFinding]:
    findings: list[PolicyFinding] = []
    for key, value in config.items():
        field = f"config.{key}"
        if key.startswith("raw."):
            findings.append(PolicyFinding("raw_config_forbidden", field))
            continue
        if key.startswith("security."):
            findings.append(PolicyFinding("host_security_setting_forbidden", field))
            continue
        if key not in _SAFE_CONFIG_KEYS and not key.startswith(_SAFE_CONFIG_PREFIXES):
            findings.append(PolicyFinding("config_key_not_allowed", field))
            continue
        if key == "limits.cpu" and not _CPU_COUNT_RE.fullmatch(str(value).strip()):
            findings.append(PolicyFinding("cpu_count_required", field))
        elif key == "limits.memory" and not _POSITIVE_SIZE_RE.fullmatch(str(value).strip()):
            findings.append(PolicyFinding("absolute_memory_limit_required", field))
        elif key == "limits.processes" and not _CPU_COUNT_RE.fullmatch(str(value).strip()):
            findings.append(PolicyFinding("positive_integer_required", field))
    return findings


def _validate_nic(field: str, device: Mapping[str, Any]) -> list[PolicyFinding]:
    findings: list[PolicyFinding] = []
    network = str(device.get("network") or "").strip()
    nic_type = str(device.get("nictype") or "").strip().lower()
    if not network:
        findings.append(PolicyFinding("managed_network_required", field))
    if device.get("parent") or nic_type in _UNMANAGED_NIC_TYPES:
        findings.append(PolicyFinding("unmanaged_nic_forbidden", field))
    if set(device) - _NIC_KEYS:
        findings.append(PolicyFinding("nic_property_not_allowed", field))
    return findings


def _validate_disk(field: str, device: Mapping[str, Any]) -> list[PolicyFinding]:
    findings: list[PolicyFinding] = []
    path = str(device.get("path") or "").strip()
    source = str(device.get("source") or "").strip()
    pool = str(device.get("pool") or "").strip()
    if set(device) - _DISK_KEYS:
        findings.append(PolicyFinding("disk_property_not_allowed", field))
    if not path.startswith("/"):
        findings.append(PolicyFinding("guest_disk_path_required", field))
    if source.startswith("/") or source in {".", ".."} or "/" in source:
        findings.append(PolicyFinding("host_path_disk_forbidden", field))
    if path != "/" and (
        not pool or not source or not _VOLUME_NAME_RE.fullmatch(source)
    ):
        findings.append(PolicyFinding("project_volume_required", field))
    size = str(device.get("size") or "").strip()
    if size and not _POSITIVE_SIZE_RE.fullmatch(size):
        findings.append(PolicyFinding("absolute_disk_size_required", field))
    return findings


def _validate_devices(
    devices: Mapping[str, Mapping[str, Any]],
) -> list[PolicyFinding]:
    findings: list[PolicyFinding] = []
    for name, device in devices.items():
        # Device names are identifiers rather than secret values, and including them makes the
        # form able to mark the exact row without echoing any device property value.
        field = f"devices.{name}"
        device_type = str(device.get("type") or "").strip().lower()
        if device_type == "none":
            if set(device) != {"type"}:
                findings.append(PolicyFinding("none_device_must_be_empty", field))
        elif device_type == "nic":
            findings.extend(_validate_nic(field, device))
        elif device_type == "disk":
            findings.extend(_validate_disk(field, device))
        elif device_type in _HOST_DEVICE_TYPES:
            findings.append(PolicyFinding("host_device_forbidden", field))
        else:
            findings.append(PolicyFinding("device_type_not_allowed", field))
    return findings


def shared_payload_findings(
    config: Mapping[str, Any],
    devices: Mapping[str, Mapping[str, Any]],
    *,
    existing_resource: bool = False,
) -> tuple[PolicyFinding, ...]:
    """Return the value-free shared-cluster findings used by every write guard.

    A daemon readback adds immutable bookkeeping such as ``volatile.*`` and image metadata to
    an instance's local config. Those keys were not supplied by the tenant and cannot grant a
    host capability, so the pre-share inventory audit ignores them. Request payloads retain the
    stricter behavior and reject every key outside the allowlist.
    """

    checked_config = config
    if existing_resource:
        checked_config = {
            key: value
            for key, value in config.items()
            if not str(key).startswith(("volatile.", "image."))
        }
    return tuple(_validate_config(checked_config) + _validate_devices(devices))


def validate_instance_payload(
    policy: ClusterWritePolicy,
    *,
    config: Mapping[str, Any],
    devices: Mapping[str, Mapping[str, Any]],
    profile_payloads: Sequence[
        tuple[Mapping[str, Any], Mapping[str, Mapping[str, Any]]]
    ] = (),
) -> None:
    """Validate an effective instance payload and optional unmerged profile bodies.

    ``config`` and ``devices`` may be the already-effective maps after profiles and local
    overrides were merged; that is sufficient and avoids duplicate profile I/O. A caller that
    still has separate profile bodies may supply them as an additional bypass check.
    """

    ensure_write_role(policy)
    if not policy.requires_safe_allowlist:
        return
    findings = list(shared_payload_findings(config, devices))
    for index, (profile_config, profile_devices) in enumerate(profile_payloads):
        findings.extend(
            PolicyFinding(finding.code, f"profiles.{index}.{finding.field}")
            for finding in _validate_config(profile_config)
        )
        findings.extend(
            PolicyFinding(finding.code, f"profiles.{index}.{finding.field}")
            for finding in _validate_devices(profile_devices)
        )
    if findings:
        raise InstanceSecurityPolicyViolation(
            "shared_cluster_policy_violation",
            "This configuration needs host-level access and is not allowed on a shared cluster.",
            findings=findings,
        )


async def validate_instance_write(
    user: AuthenticatedUser,
    cluster_id: str,
    db: AsyncSession,
    *,
    config: Mapping[str, Any],
    devices: Mapping[str, Mapping[str, Any]],
    profile_payloads: Sequence[
        tuple[Mapping[str, Any], Mapping[str, Mapping[str, Any]]]
    ] = (),
) -> ClusterWritePolicy:
    """Resolve current ownership and validate an instance write in one request-safe call."""

    policy = await resolve_cluster_write_policy(user, cluster_id, db)
    validate_instance_payload(
        policy,
        config=config,
        devices=devices,
        profile_payloads=profile_payloads,
    )
    return policy


def validate_profile_payload(
    policy: ClusterWritePolicy,
    *,
    config: Mapping[str, Any],
    devices: Mapping[str, Mapping[str, Any]],
) -> None:
    validate_instance_payload(policy, config=config, devices=devices)


_SAFE_PROJECT_FEATURES = {
    "features.images",
    "features.networks",
    "features.profiles",
    "features.storage.buckets",
    "features.storage.volumes",
}
_REQUIRED_SHARED_PROJECT_FEATURES = {
    "features.images",
    "features.networks",
    "features.profiles",
    "features.storage.volumes",
}


def shared_project_findings(config: Mapping[str, Any]) -> tuple[PolicyFinding, ...]:
    """Describe missing isolation required before a cluster can gain another owner.

    Only isolation invariants are inspected here. Existing projects may carry ordinary
    project quotas and metadata that are unrelated to host escape, while project mutation is
    still governed by the stricter create/update policy below.
    """

    findings: list[PolicyFinding] = []
    if str(config.get("restricted") or "").strip().lower() != "true":
        findings.append(
            PolicyFinding("restricted_project_required", "config.restricted")
        )
    for key in sorted(_REQUIRED_SHARED_PROJECT_FEATURES):
        if str(config.get(key) or "").strip().lower() != "true":
            findings.append(
                PolicyFinding("isolated_project_feature_required", f"config.{key}")
            )
    return tuple(findings)


def validate_target_project_isolation(
    policy: ClusterWritePolicy,
    config: Mapping[str, Any],
) -> None:
    """Admit a write only when its shared-cluster project proves tenant isolation."""

    ensure_write_role(policy)
    if not policy.requires_safe_allowlist:
        return
    findings = shared_project_findings(config)
    if findings:
        raise InstanceSecurityPolicyViolation(
            "shared_cluster_project_not_isolated",
            (
                "This project is not isolated for shared-cluster use. An Admin must review "
                "and restrict it before Partner instance writes are allowed."
            ),
            status_code=409,
            findings=findings,
        )


def prepare_project_create_config(
    policy: ClusterWritePolicy,
    config: Mapping[str, str],
) -> dict[str, str]:
    """Return the project config with shared-cluster isolation fixed by the server."""

    ensure_write_role(policy)
    result = dict(config)
    if not policy.requires_safe_allowlist:
        return result

    findings: list[PolicyFinding] = []
    for key, value in result.items():
        field = f"config.{key}"
        if key in _SAFE_PROJECT_FEATURES:
            normalized = str(value).strip().lower()
            if normalized not in {"true", "false"}:
                findings.append(PolicyFinding("boolean_required", field))
            elif key in _REQUIRED_SHARED_PROJECT_FEATURES and normalized != "true":
                findings.append(PolicyFinding("isolated_project_feature_required", field))
        elif key == "restricted":
            if str(value).strip().lower() != "true":
                findings.append(PolicyFinding("restricted_project_required", field))
        elif key.startswith("user."):
            continue
        else:
            findings.append(PolicyFinding("project_config_key_not_allowed", field))
    if findings:
        raise InstanceSecurityPolicyViolation(
            "shared_cluster_project_policy_violation",
            "This project configuration is not allowed on a shared cluster.",
            findings=findings,
        )
    for key in _REQUIRED_SHARED_PROJECT_FEATURES:
        result[key] = "true"
    result["restricted"] = "true"
    return result


def validate_project_update(
    policy: ClusterWritePolicy,
    current_config: Mapping[str, Any],
) -> None:
    """Refuse writes to legacy shared projects until an Admin isolates them explicitly."""

    validate_target_project_isolation(policy, current_config)
