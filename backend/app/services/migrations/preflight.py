"""Side-effect-free migration discovery and compatibility checks.

Every discovered project resource is either represented in the plan or becomes a blocker.
There is intentionally no best-effort project migration mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

from app.core.lxd_path import safe_segment
from app.services.lxd import mobility
from app.services.lxd.project_usage import parse_lxd_size

from . import project_policy
from .billing import UNMEASURED_SIZE_WARNING
from .build_policy import build_verdict
from .security import (
    MigrationSecurityError,
    api_endpoint,
    approved_build,
    approved_lxd_native_cross_project_move,
    canonical_json_hash,
    daemon_identity,
)


_PROJECT_DEPENDENCY_FEATURES = (
    "features.images",
    "features.networks",
    "features.networks.zones",
    "features.profiles",
    "features.storage.buckets",
    "features.storage.volumes",
)
_OPTIONAL_COLLECTION_EXTENSIONS = {
    "network_acls": "network_acl",
    "network_zones": "network_dns",
    "network_address_sets": "network_address_set",
    "placement_groups": "placement_groups",
    "storage_buckets": "storage_buckets",
    "network_forwards": "network_forward",
    "network_load_balancers": "network_load_balancer",
}
_PROJECT_OPERATION_ROOTS = {
    "containers": frozenset({"containers", "instances"}),
    "images": frozenset({"images"}),
    "instance-backups": frozenset(
        {"containers", "instances", "virtual-machines"}
    ),
    "instance-snapshots": frozenset(
        {"containers", "instances", "virtual-machines"}
    ),
    "instances": frozenset({"containers", "instances", "virtual-machines"}),
    "network-acls": frozenset({"network-acls"}),
    "network-address-sets": frozenset({"network-address-sets"}),
    "network-zones": frozenset({"network-zones"}),
    "networks": frozenset({"networks"}),
    "profiles": frozenset({"profiles"}),
    "projects": frozenset({"projects"}),
    "virtual-machines": frozenset({"instances", "virtual-machines"}),
}
_MAX_OPERATION_RESOURCE_URL_LENGTH = 4096
_TERMINAL_OPERATION_BUCKETS = frozenset({"cancelled", "failure", "success"})


class InventoryReadError(RuntimeError):
    """The daemon did not provide authoritative inventory evidence."""

    def __init__(self, resource: str) -> None:
        super().__init__(f"Authoritative {resource} inventory is unavailable.")
        self.resource = resource


@dataclass
class PreflightResult:
    strategy: str
    downtime: str
    capabilities: dict[str, Any] = field(default_factory=dict)
    resources: list[dict[str, Any]] = field(default_factory=list)
    blockers: list[dict[str, str]] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)
    # In-memory target contracts by source instance name. Never persisted or serialized:
    # they carry raw config values that only the planner's policy checks may read.
    contracts: dict[str, Any] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return not self.blockers

    def block(self, code: str, message: str, remediation: str = "") -> None:
        self.blockers.append(
            {"code": code, "message": message, "remediation": remediation}
        )

    def warn(self, code: str, message: str, remediation: str = "") -> None:
        self.warnings.append(
            {"code": code, "message": message, "remediation": remediation}
        )


def _metadata(response: Any, *, resource: str) -> Any:
    status_code = getattr(response, "status_code", 200)
    if not isinstance(status_code, int) or status_code != 200:
        raise InventoryReadError(resource)
    try:
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        raise InventoryReadError(resource) from exc
    if not isinstance(payload, dict) or "metadata" not in payload:
        raise InventoryReadError(resource)
    return payload["metadata"]


def _is_not_found(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    return response is not None and getattr(response, "status_code", None) == 404


def _get_collection(node: Any, *, project: str, resource: str) -> list[dict[str, Any]]:
    try:
        response = node.get(params={"project": project, "recursion": 1})
    except Exception as exc:  # noqa: BLE001
        raise InventoryReadError(resource) from exc
    metadata = _metadata(response, resource=resource)
    if not isinstance(metadata, list) or any(
        not isinstance(item, dict) for item in metadata
    ):
        raise InventoryReadError(resource)
    return metadata


def _authoritative_extensions(client: Any) -> set[str]:
    """Return extensions only when `/1.0` proves an exact string collection."""
    identity = daemon_identity(client)
    extensions = identity.get("extensions")
    if not isinstance(extensions, list) or any(
        not isinstance(extension, str) or not extension for extension in extensions
    ):
        raise InventoryReadError("api_extensions")
    host_info = getattr(client, "host_info", None)
    if isinstance(host_info, dict):
        raw_extensions = host_info.get("api_extensions")
        if not isinstance(raw_extensions, list) or any(
            not isinstance(extension, str) or not extension
            for extension in raw_extensions
        ):
            raise InventoryReadError("api_extensions")
        if set(raw_extensions) != set(extensions):
            raise InventoryReadError("api_extensions")
    return set(extensions)


def _operation_resource_project(kind: str, raw_url: Any) -> str:
    if (
        not isinstance(kind, str)
        or not isinstance(raw_url, str)
        or not raw_url
        or len(raw_url) > _MAX_OPERATION_RESOURCE_URL_LENGTH
    ):
        raise InventoryReadError("active operation ownership")
    parsed = urlparse(raw_url)
    if parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith("/1.0/"):
        raise InventoryReadError("active operation ownership")
    segments = [unquote(segment) for segment in parsed.path.split("/") if segment]
    if (
        len(segments) < 3
        or segments[0] != "1.0"
        or any(segment in {".", ".."} or "/" in segment for segment in segments)
    ):
        raise InventoryReadError("active operation ownership")
    normalized_kind = kind.replace("_", "-")
    root = segments[1]
    if normalized_kind in {"storage-volumes", "storage-volume-snapshots"}:
        if root != "storage-pools" or len(segments) < 6 or segments[3] != "volumes":
            raise InventoryReadError("active operation ownership")
    elif normalized_kind == "storage-buckets":
        if root != "storage-pools" or len(segments) < 5 or segments[3] != "buckets":
            raise InventoryReadError("active operation ownership")
    elif root not in _PROJECT_OPERATION_ROOTS.get(normalized_kind, frozenset()):
        raise InventoryReadError("active operation ownership")
    try:
        query = parse_qs(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=False,
            max_num_fields=32,
        )
    except ValueError as exc:
        raise InventoryReadError("active operation ownership") from exc
    project_values = query.get("project", [])
    if len(project_values) > 1 or (project_values and not project_values[0].strip()):
        raise InventoryReadError("active operation ownership")
    if normalized_kind == "projects":
        project = segments[2].strip()
        if project_values and project_values[0] != project:
            raise InventoryReadError("active operation ownership")
    else:
        project = project_values[0].strip() if project_values else "default"
    if not project or project in {".", ".."} or "/" in project:
        raise InventoryReadError("active operation ownership")
    return project


def _active_operation_projects(response: Any) -> set[str]:
    metadata = _metadata(response, resource="active operation")
    if not isinstance(metadata, dict):
        raise InventoryReadError("active operation")
    active: set[str] = set()
    for bucket, operations in metadata.items():
        if not isinstance(bucket, str) or not isinstance(operations, list) or any(
            not isinstance(operation, dict) for operation in operations
        ):
            raise InventoryReadError("active operation")
        if bucket.strip().lower() in _TERMINAL_OPERATION_BUCKETS:
            continue
        for operation in operations:
            status_code = operation.get("status_code")
            if not isinstance(status_code, int) or not 100 <= status_code < 200:
                raise InventoryReadError("active operation status")
            resources = operation.get("resources")
            if not isinstance(resources, dict) or not resources:
                raise InventoryReadError("active operation ownership")
            owners: set[str] = set()
            resource_count = 0
            for kind, urls in resources.items():
                if not isinstance(kind, str) or not isinstance(urls, list) or not urls:
                    raise InventoryReadError("active operation ownership")
                for raw_url in urls:
                    owners.add(_operation_resource_project(kind, raw_url))
                    resource_count += 1
            if resource_count == 0 or len(owners) != 1:
                raise InventoryReadError("active operation ownership")
            active.update(owners)
    return active


def _read_active_operation_projects(client: Any, projects: set[str]) -> set[str]:
    extensions = _authoritative_extensions(client)
    try:
        operations_node = client.api.operations
    except (AttributeError, TypeError) as exc:
        raise InventoryReadError("active operation") from exc
    if "operations_get_query_all_projects" in extensions:
        try:
            response = operations_node.get(
                params={"recursion": 1, "all-projects": "true"}
            )
        except Exception as exc:  # noqa: BLE001
            raise InventoryReadError("active operation") from exc
        return _active_operation_projects(response) & projects

    active: set[str] = set()
    for project in sorted(projects):
        try:
            response = operations_node.get(
                params={"project": project, "recursion": 1}
            )
        except Exception as exc:  # noqa: BLE001
            raise InventoryReadError("active operation") from exc
        observed = _active_operation_projects(response)
        if any(owner != project for owner in observed):
            raise InventoryReadError("active operation ownership")
        active.update(observed)
    return active


def _check_active_operations(
    result: PreflightResult,
    *,
    source_client: Any,
    target_client: Any,
    source_cluster: str,
    source_project: str,
    target_cluster: str,
    target_project: str,
) -> None:
    endpoints = [
        ("source", source_client, {source_project}),
    ]
    if source_cluster == target_cluster:
        endpoints[0][2].add(target_project)
    else:
        endpoints.append(("target", target_client, {target_project}))
    for side, client, projects in endpoints:
        try:
            active = _read_active_operation_projects(client, projects)
        except InventoryReadError as exc:
            result.capabilities["daemon_operation_inventory_verified"] = False
            result.capabilities["daemon_operations_quiescent"] = False
            _block_inventory_error(result, side=side, error=exc)
            continue
        for project in sorted(active):
            result.capabilities["daemon_operations_quiescent"] = False
            result.block(
                f"{side}_active_daemon_operation",
                f"The {side} daemon has an active operation touching project '{project}'.",
                "Wait for the daemon operation to settle and run preflight again. Reconcile an "
                "unknown outcome before migration; active operations are never assumed safe.",
            )


def _get_project(client: Any, project: str) -> Optional[dict[str, Any]]:
    try:
        response = client.api.projects[safe_segment(project)].get()
    except Exception as exc:
        if _is_not_found(exc):
            return None
        raise InventoryReadError(f"project '{project}'") from exc
    metadata = _metadata(response, resource=f"project '{project}'")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("config"), dict):
        raise InventoryReadError(f"project '{project}'")
    return metadata


def _validate_instance_metadata(metadata: Any, resource: str) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        raise InventoryReadError(resource)
    for key in ("config", "devices", "expanded_config", "expanded_devices"):
        if key in metadata and not isinstance(metadata[key], dict):
            raise InventoryReadError(resource)
    profiles = metadata.get("profiles")
    if profiles is not None and (
        not isinstance(profiles, list)
        or any(not isinstance(profile, str) for profile in profiles)
    ):
        raise InventoryReadError(resource)
    return metadata


def _instance(client: Any, project: str, name: str) -> Optional[dict[str, Any]]:
    try:
        response = client.api.instances[safe_segment(name)].get(
            params={"project": project}
        )
    except Exception as exc:
        if _is_not_found(exc):
            return None
        raise InventoryReadError(f"instance '{name}'") from exc
    return _validate_instance_metadata(
        _metadata(response, resource=f"instance '{name}'"),
        f"instance '{name}'",
    )


def _profile(client: Any, project: str, name: str) -> Optional[dict[str, Any]]:
    try:
        response = client.api.profiles[safe_segment(name)].get(
            params={"project": project}
        )
    except Exception as exc:
        if _is_not_found(exc):
            return None
        raise InventoryReadError(f"profile '{name}'") from exc
    metadata = _metadata(response, resource=f"profile '{name}'")
    if not isinstance(metadata, dict) or any(
        key in metadata and not isinstance(metadata[key], dict)
        for key in ("config", "devices")
    ):
        raise InventoryReadError(f"profile '{name}'")
    return metadata


def _storage_pools(client: Any) -> list[str]:
    try:
        response = client.api.storage_pools.get()
    except Exception as exc:  # noqa: BLE001
        raise InventoryReadError("storage pool") from exc
    metadata = _metadata(response, resource="storage pool")
    if not isinstance(metadata, list):
        raise InventoryReadError("storage pool")
    pools: list[str] = []
    for value in metadata:
        if not isinstance(value, str):
            raise InventoryReadError("storage pool")
        name = value.rstrip("/").rsplit("/", 1)[-1]
        if not name:
            raise InventoryReadError("storage pool")
        pools.append(name)
    return pools


def _shared_dependency_features(
    project: str, project_record: Optional[dict[str, Any]]
) -> list[str]:
    if project == "default" or project_record is None:
        return []
    config = project_record.get("config") or {}
    if not isinstance(config, dict):
        return list(_PROJECT_DEPENDENCY_FEATURES)
    return [
        feature
        for feature in _PROJECT_DEPENDENCY_FEATURES
        if str(config.get(feature, "false")).strip().lower() != "true"
    ]


def _block_inventory_error(
    result: PreflightResult, *, side: str, error: InventoryReadError
) -> None:
    result.block(
        f"{side}_inventory_unverified",
        f"The {side} daemon did not provide authoritative {error.resource} inventory.",
        "Restore the endpoint or daemon capability response and run preflight again. "
        "Unknown inventory is never treated as empty.",
    )


def _project_fingerprint(project: Optional[dict[str, Any]]) -> Optional[str]:
    """Policy-only hash. Operator ``user.*`` metadata never changes migration approval."""
    return project_policy.policy_fingerprint(project)


def _instance_preflight_fingerprint(instance: dict[str, Any]) -> str:
    """Hash approval-relevant state without retaining raw guest configuration."""
    config = {
        key: value
        for key, value in (
            instance.get("expanded_config") or instance.get("config") or {}
        ).items()
        if not str(key).startswith("volatile.")
    }
    return canonical_json_hash(
        {
            "type": instance.get("type"),
            "architecture": instance.get("architecture"),
            "profiles": instance.get("profiles") or [],
            "config": config,
            "devices": instance.get("expanded_devices")
            or instance.get("devices")
            or {},
            "location": instance.get("location"),
        }
    )


def _apply_lxd_native_cross_project_move_gate(
    result: PreflightResult,
    *,
    intent: str,
    same_cluster: bool,
    source_project: str,
    target_project: str,
    source_identity: dict[str, Any],
    settings: Any,
) -> None:
    """Expose the native-move baseline without selecting the GHSA-affected path."""
    applicable = bool(
        intent == "move"
        and same_cluster
        and source_project != target_project
        and source_identity["engine"] == "lxd"
    )
    security_approved = bool(
        applicable
        and approved_lxd_native_cross_project_move(source_identity, settings)
    )
    result.capabilities["lxd_native_cross_project_move_applicable"] = applicable
    result.capabilities["lxd_native_cross_project_move_security_approved"] = (
        security_approved
    )
    result.capabilities["lxd_native_cross_project_move_selected"] = False


def _check_capacity(
    result: PreflightResult,
    *,
    source_client: Any,
    target_client: Any,
    source_project: str,
    instance_names: list[str],
    target_pool: Optional[str],
    target_member: Optional[str] = None,
) -> None:
    """Require source allocation and target free space, including snapshot overhead."""
    if not target_pool:
        result.block(
            "target_pool_mapping_required",
            "A target root storage pool mapping is required before capacity can be validated.",
            "Map storage_pools.root or storage_pools.* to a managed target pool.",
        )
        return
    footprints = [
        _instance_transfer_bytes(source_client, source_project, name)
        for name in instance_names
    ]
    if target_member:
        try:
            headroom = _member_pool_headroom(
                target_client, target_pool, target_member
            )
        except InventoryReadError as exc:
            _block_inventory_error(result, side="target", error=exc)
            return
    else:
        try:
            target_pools = _storage_pools(target_client)
        except InventoryReadError as exc:
            _block_inventory_error(result, side="target", error=exc)
            return
        if target_pool not in target_pools:
            result.block(
                "target_pool_missing", f"Target storage pool '{target_pool}' does not exist."
            )
            return
        headroom = mobility.pool_headroom(target_client, target_pool)
    bounds, bounded = _bound_unmeasured_footprints(
        result,
        client=source_client,
        project=source_project,
        instance_names=instance_names,
        footprints=footprints,
    )
    if any(value is None for value in footprints):
        unstarted = [
            name
            for name, value in zip(instance_names, footprints)
            if value is None and mobility.instance_footprint(source_client, name, source_project) == 0
        ]
        if unstarted:
            result.block(
                "source_footprint_unverified",
                f"{', '.join(unstarted)} report no disk usage yet, which LXD does for an instance "
                "that was never started, so the transfer cannot be sized.",
                "Start and stop the instance once, or give its root disk an explicit size, then run "
                "preflight again.",
            )
        else:
            result.block(
                "source_footprint_unverified",
                "One or more source disk allocations or snapshot sizes could not be measured.",
                "Restore authoritative disk and snapshot size reporting, then run preflight again.",
            )
    if headroom is None:
        result.block(
            "target_capacity_unverified",
            f"Free space for target pool '{target_pool}' could not be measured.",
            "Restore authoritative target pool capacity reporting, then run preflight again.",
        )
        return
    if all(value is not None for value in footprints):
        bound = sum(bounds.values())
        measured = sum(int(value or 0) for value in footprints)
        free = int(headroom["free"])
        if bound and measured <= free < measured + bound:
            # The owner's choice (2026-10-29): a dir pool's bound is everything it holds, which a
            # copy within a well-filled pool (Pandora's) never fits, so an operator may accept the
            # unmeasured size instead of being refused. What was measured must still fit.
            result.warnings[:] = [
                warning for warning in result.warnings if warning.get("code") != "source_footprint_bounded_by_pool"
            ]
            result.warn(
                UNMEASURED_SIZE_WARNING,
                f"The size of {', '.join(bounded)} cannot be measured on its dir pool, and target pool "
                f"'{target_pool}' has less free space than that pool holds in total. The transfer may "
                "fill the target pool.",
                "Check the target pool's free space, then confirm the unmeasured size at approval, or "
                "choose a pool with more room.",
            )
        elif measured + bound > free:
            message = f"Target pool '{target_pool}' has insufficient measured free space."
            if bound:
                message = (
                    f"Target pool '{target_pool}' has {_gib(free)} free, and the measured part of the "
                    f"transfer alone needs {_gib(measured)}."
                )
            result.block(
                "target_pool_insufficient_capacity",
                message,
                "Free capacity or choose another mapped pool.",
            )


# Drivers that report no per-volume usage without project quotas: LXD answers 0 for every volume on
# them, so a 0 from one is not a measurement.
_UNMEASURED_USAGE_DRIVERS = frozenset({"dir"})


def _gib(value: int) -> str:
    return f"{value / 1024**3:.1f} GiB"


def _unmeasured_pool_bound(
    client: Any, project: str, name: str
) -> Optional[tuple[tuple[str, str], int]]:
    """((pool, member), used bytes) when the instance's root sits on a driver that cannot size it.

    Everything the pool holds bounds what the instance and its snapshots can need, which is the
    owner's choice (2026-10-29) for dir pools over refusing them outright.
    """
    instance = _instance(client, project, name)
    if not isinstance(instance, dict):
        return None
    devices = instance.get("expanded_devices") or instance.get("devices") or {}
    pool = next(
        (
            device.get("pool")
            for device in devices.values()
            if isinstance(device, dict) and device.get("type") == "disk" and device.get("path") == "/"
        ),
        None,
    )
    if not isinstance(pool, str) or not pool:
        return None
    detail = _metadata(client.api.storage_pools[safe_segment(pool)].get(), resource=f"storage pool '{pool}'")
    if not isinstance(detail, dict) or detail.get("driver") not in _UNMEASURED_USAGE_DRIVERS:
        return None
    member = str(instance.get("location") or "")
    if member and member != "none":
        headroom = _member_pool_headroom(client, pool, member)
    else:
        member = ""
        headroom = mobility.pool_headroom(client, pool)
    if not headroom:
        return None
    return (pool, member), int(headroom["used"])


def _bound_unmeasured_footprints(
    result: PreflightResult,
    *,
    client: Any,
    project: str,
    instance_names: list[str],
    footprints: list[Optional[int]],
) -> tuple[dict[tuple[str, str], int], list[str]]:
    """Replace unmeasurable dir footprints with their pool's usage, counted once per pool.

    Returns the bound per (pool, member) and the instances it stands in for.
    """
    bounds: dict[tuple[str, str], int] = {}
    bounded: list[str] = []
    for index, name in enumerate(instance_names):
        if footprints[index] is not None:
            continue
        try:
            found = _unmeasured_pool_bound(client, project, name)
        except Exception:  # noqa: BLE001 - an unreadable pool leaves the footprint unmeasured
            found = None
        if found is None:
            continue
        key, used = found
        bounds[key] = used
        footprints[index] = 0
        bounded.append(name)
    if bounds:
        # note: no figures in a warning. It is part of the approved report's hash, and a pool's
        # usage moves between plan and execute, which would ask for a new approval every time.
        pools = ", ".join(sorted({f"'{pool}'" for pool, _member in bounds}))
        result.warn(
            "source_footprint_bounded_by_pool",
            f"{', '.join(bounded)} {'sits' if len(bounded) == 1 else 'sit'} on a dir storage pool, "
            "which does not report how much each "
            f"instance uses, so the transfer is sized by everything pool {pools} holds.",
            "Nothing to do when the target has room for that. A pool that reports usage (zfs, "
            "btrfs, lvm) gives an exact size.",
        )
    return bounds, bounded


def _instance_transfer_bytes(
    client: Any, project: str, name: str
) -> Optional[int]:
    """Conservatively include live usage, provisioned root size, and every snapshot."""
    usage = mobility.instance_footprint(client, name, project)
    if usage is None:
        return None
    try:
        instance = _instance(client, project, name)
        if instance is None:
            return None
        devices = instance.get("expanded_devices") or instance.get("devices") or {}
        root = next(
            (
                device
                for device in devices.values()
                if isinstance(device, dict)
                and device.get("type") == "disk"
                and device.get("path") == "/"
            ),
            None,
        )
        if not isinstance(root, dict):
            return None
        provisioned = parse_lxd_size(root.get("size"))
        if usage == 0 and provisioned is None:
            # important: 0 is what LXD reports for an instance that was never started (on zfs a
            # clone of its image with no bytes of its own), yet a transfer still writes the whole
            # root. Without an explicit root size there is nothing honest to size it by, so it
            # stays unmeasured; _check_capacity says why and how to fix it.
            return None
        current = max(usage, provisioned or 0)
        snapshots = _get_collection(
            client.api.instances[safe_segment(name)].snapshots,
            project=project,
            resource=f"snapshot size for '{name}'",
        )
        snapshot_bytes = 0
        for snapshot in snapshots:
            raw_size = snapshot.get("size")
            if raw_size is None:
                raw_size = snapshot.get("size_bytes")
            size = parse_lxd_size(raw_size)
            if size is None:
                return None
            snapshot_bytes += size
        return current + snapshot_bytes
    except Exception:  # noqa: BLE001
        return None


def _member_pool_headroom(
    client: Any, pool: str, member: str
) -> dict[str, int]:
    try:
        detail = _metadata(
            client.api.storage_pools[safe_segment(pool)].get(
                params={"target": member}
            ),
            resource=f"storage pool '{pool}' on member '{member}'",
        )
        resources = _metadata(
            client.api.storage_pools[safe_segment(pool)].resources.get(
                params={"target": member}
            ),
            resource=f"storage capacity for '{pool}' on member '{member}'",
        )
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, InventoryReadError):
            raise
        raise InventoryReadError(
            f"storage pool '{pool}' on member '{member}'"
        ) from exc
    if not isinstance(detail, dict) or str(detail.get("status") or "").lower() not in {
        "created",
        "available",
    }:
        raise InventoryReadError(f"storage pool '{pool}' on member '{member}'")
    space = resources.get("space") if isinstance(resources, dict) else None
    if not isinstance(space, dict):
        raise InventoryReadError(f"storage capacity for '{pool}' on member '{member}'")
    total = space.get("total")
    used = space.get("used")
    if not isinstance(total, int) or not isinstance(used, int) or total <= 0 or used < 0:
        raise InventoryReadError(f"storage capacity for '{pool}' on member '{member}'")
    return {"total": total, "used": used, "free": max(0, total - used)}


def _member_relocation_checks(
    result: PreflightResult,
    *,
    client: Any,
    project: str,
    target_member: str,
    target_pool: Optional[str],
    instances: list[dict[str, Any]],
) -> None:
    """Prove one exact online member can satisfy local storage and NIC dependencies."""
    root_pools: set[str] = set()
    for instance in instances:
        devices = instance.get("expanded_devices") or instance.get("devices") or {}
        roots = [
            device
            for device in devices.values()
            if isinstance(device, dict)
            and device.get("type") == "disk"
            and device.get("path") == "/"
        ] if isinstance(devices, dict) else []
        if len(roots) != 1 or not isinstance(roots[0].get("pool"), str) or not roots[0].get("pool"):
            result.block(
                "source_root_pool_unverified",
                f"The effective root pool for instance '{instance.get('name')}' is unreadable.",
                "Restore expanded root-disk inventory and run preflight again.",
            )
            continue
        root_pools.add(str(roots[0]["pool"]))
    if len(root_pools) != 1:
        result.block(
            "member_relocation_root_pool_ambiguous",
            "Member relocation requires one exact effective root pool.",
            "Relocate instances individually after each root pool is verified.",
        )
        return
    effective_root_pool = next(iter(root_pools))
    if target_pool is not None and target_pool != effective_root_pool:
        result.block(
            "member_relocation_pool_mapping_unsupported",
            f"Native member relocation preserves root pool '{effective_root_pool}' and cannot "
            f"apply requested pool '{target_pool}'.",
            "Remove the pool mapping or map it exactly to the effective root pool.",
        )
    result.capabilities["member_relocation_root_pool"] = effective_root_pool
    try:
        response = client.api.cluster.members.get(params={"recursion": 1})
        metadata = _metadata(response, resource="cluster member")
    except Exception as exc:  # noqa: BLE001
        error = exc if isinstance(exc, InventoryReadError) else InventoryReadError(
            "cluster member"
        )
        _block_inventory_error(result, side="target", error=error)
        return
    if not isinstance(metadata, list) or any(not isinstance(row, dict) for row in metadata):
        _block_inventory_error(
            result,
            side="target",
            error=InventoryReadError("cluster member"),
        )
        return
    matches = [
        row
        for row in metadata
        if row.get("server_name") == target_member or row.get("name") == target_member
    ]
    if len(matches) != 1:
        result.block(
            "target_member_unverified",
            f"Target member '{target_member}' was not found uniquely in cluster inventory.",
            "Select an exact online cluster member and run preflight again.",
        )
        return
    member = matches[0]
    if str(member.get("status") or "").lower() != "online":
        result.block(
            "target_member_offline",
            f"Target member '{target_member}' is not online.",
            "Restore the member to Online state or choose another member.",
        )
    member_architecture = member.get("architecture")
    if not isinstance(member_architecture, str) or not member_architecture:
        result.block(
            "target_member_architecture_unverified",
            f"Target member '{target_member}' did not report an architecture.",
            "Restore authoritative member inventory before relocation.",
        )
    else:
        mismatches = sorted(
            {
                str(instance.get("architecture"))
                for instance in instances
                if instance.get("architecture")
                and instance.get("architecture") != member_architecture
            }
        )
        if mismatches:
            result.block(
                "target_member_architecture_mismatch",
                f"Target member '{target_member}' cannot run: {', '.join(mismatches)}.",
                "Choose a member with the same workload architecture.",
            )

    for instance in instances:
        name = str(instance.get("name") or "")
        devices = instance.get("expanded_devices") or instance.get("devices") or {}
        if not isinstance(devices, dict):
            result.block(
                "target_member_devices_unverified",
                f"Effective devices for instance '{name}' are unreadable.",
                "Restore expanded device inventory and run preflight again.",
            )
            continue
        for device_name, device in devices.items():
            if not isinstance(device, dict) or device.get("type") != "nic":
                continue
            network = device.get("network")
            nictype = str(device.get("nictype") or "").strip().lower()
            parent = device.get("parent")
            if not network:
                if parent or nictype:
                    result.block(
                        "target_member_host_network_unverified",
                        f"NIC '{device_name}' on '{name}' depends on host-local parent or "
                        "uplink configuration that Orcastra cannot prove on the target member.",
                        "Use a managed network proven on the target member, or review the "
                        "relocation outside this release.",
                    )
                continue
            try:
                network_record = _metadata(
                    client.api.networks[safe_segment(str(network))].get(
                        params={"project": project, "target": target_member}
                    ),
                    resource=f"network '{network}' on member '{target_member}'",
                )
            except Exception as exc:  # noqa: BLE001
                error = (
                    exc
                    if isinstance(exc, InventoryReadError)
                    else InventoryReadError(
                        f"network '{network}' on member '{target_member}'"
                    )
                )
                _block_inventory_error(result, side="target", error=error)
                continue
            locations = (
                network_record.get("locations")
                if isinstance(network_record, dict)
                else None
            )
            status = (
                str(network_record.get("status") or "").lower()
                if isinstance(network_record, dict)
                else ""
            )
            if (
                not isinstance(network_record, dict)
                or not isinstance(locations, list)
                or target_member not in locations
                or status not in {"created", "available"}
            ):
                result.block(
                    "target_member_network_unavailable",
                    f"Managed network '{network}' is not proven available on member "
                    f"'{target_member}'.",
                    "Create or repair the network on that member, then run preflight again.",
                )

    _check_capacity(
        result,
        source_client=client,
        target_client=client,
        source_project=project,
        instance_names=[str(instance.get("name")) for instance in instances],
        target_pool=effective_root_pool,
        target_member=target_member,
    )


def _project_custom_volumes(client: Any, project: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for pool in _storage_pools(client):
        try:
            node = client.api.storage_pools[safe_segment(pool)].volumes.custom
        except (AttributeError, TypeError) as exc:
            raise InventoryReadError(f"custom volume in pool '{pool}'") from exc
        for metadata in _get_collection(
            node, project=project, resource=f"custom volume in pool '{pool}'"
        ):
            rows.append({"pool": pool, **metadata})
    return rows


def _project_buckets(
    client: Any, project: str, *, extensions: set[str]
) -> list[dict[str, Any]]:
    if _OPTIONAL_COLLECTION_EXTENSIONS["storage_buckets"] not in extensions:
        return []
    rows: list[dict[str, Any]] = []
    for pool in _storage_pools(client):
        try:
            node = client.api.storage_pools[safe_segment(pool)].buckets
        except (AttributeError, TypeError) as exc:
            raise InventoryReadError(f"storage bucket in pool '{pool}'") from exc
        for metadata in _get_collection(
            node, project=project, resource=f"storage bucket in pool '{pool}'"
        ):
            rows.append({"pool": pool, **metadata})
    return rows


def _known_project_resources(client: Any, project: str) -> dict[str, list[Any]]:
    extensions = _authoritative_extensions(client)
    try:
        networks_node = client.api.networks
        instances_node = client.api.instances
        profiles_node = client.api.profiles
        images_node = client.api.images
    except (AttributeError, TypeError) as exc:
        raise InventoryReadError("core project resource") from exc
    networks = _get_collection(networks_node, project=project, resource="network")
    instances = _get_collection(instances_node, project=project, resource="instance")
    for instance in instances:
        _validate_instance_metadata(instance, "instance")
        if not isinstance(instance.get("name"), str) or not instance["name"]:
            raise InventoryReadError("instance")
    resources: dict[str, list[Any]] = {
        "instances": instances,
        "profiles": _get_collection(profiles_node, project=project, resource="profile"),
        "networks": networks,
        "images": _get_collection(images_node, project=project, resource="image"),
        "custom_volumes": _project_custom_volumes(client, project),
        "storage_buckets": _project_buckets(
            client, project, extensions=extensions
        ),
    }
    for label in (
        "network_acls",
        "network_zones",
        "network_address_sets",
        "placement_groups",
    ):
        required_extension = _OPTIONAL_COLLECTION_EXTENSIONS[label]
        if required_extension not in extensions:
            resources[label] = []
            continue
        try:
            # note: LXD paths use dashes. pylxd only converts a few attribute names, so
            # ``client.api.network_acls`` would ask for /1.0/network_acls and get a 404.
            node = client.api[safe_segment(label.replace("_", "-"))]
        except (AttributeError, KeyError, TypeError) as exc:
            raise InventoryReadError(label.replace("_", " ")) from exc
        resources[label] = _get_collection(
            node, project=project, resource=label.replace("_", " ")
        )
    resources["network_forwards"] = []
    resources["network_load_balancers"] = []
    for network in networks:
        if not isinstance(network, dict) or not network.get("name"):
            continue
        if not network.get("managed"):
            # A host interface has no forwards or load balancers; LXD answers 404 for it.
            continue
        try:
            node = networks_node[safe_segment(str(network["name"]))]
        except (AttributeError, KeyError, TypeError) as exc:
            raise InventoryReadError(
                f"network '{network['name']}' dependency"
            ) from exc
        for label, attribute in (
            ("network_forwards", "forwards"),
            ("network_load_balancers", "load-balancers"),
        ):
            required_extension = _OPTIONAL_COLLECTION_EXTENSIONS[label]
            if required_extension not in extensions:
                continue
            if label == "network_load_balancers" and network.get("type") != "ovn":
                # LXD refuses the listing outright on other drivers, which cannot hold any.
                continue
            try:
                collection = node[safe_segment(attribute)]
            except (AttributeError, KeyError, TypeError) as exc:
                raise InventoryReadError(label.replace("_", " ")) from exc
            resources[label].extend(
                _get_collection(
                    collection, project=project, resource=label.replace("_", " ")
                )
            )
    return resources


def project_inventory(client: Any, project: str) -> dict[str, Any]:
    """Safe inventory projection used by cleanup drift checks."""
    resources = _known_project_resources(client, project)
    return {
        "instances": sorted(
            str(row.get("name"))
            for row in resources.get("instances", [])
            if isinstance(row, dict) and row.get("name")
        ),
        "counts": {kind: len(rows) for kind, rows in resources.items()},
    }


def _occupied_resources(resources: dict[str, list[Any]]) -> dict[str, int]:
    occupied: dict[str, int] = {}
    for kind, rows in resources.items():
        if not rows:
            continue
        if kind == "profiles":
            nonempty = [
                profile
                for profile in rows
                if not isinstance(profile, dict)
                or profile.get("name") != "default"
                or bool(profile.get("config") or {})
                or bool(profile.get("devices") or {})
            ]
            if nonempty:
                occupied[kind] = len(nonempty)
        else:
            occupied[kind] = len(rows)
    return occupied


def inspect(
    *,
    scope: str,
    intent: str,
    source_client: Any,
    target_client: Any,
    source_cluster: str,
    source_project: str,
    source_name: Optional[str],
    target_cluster: str,
    target_project: str,
    target_name: Optional[str],
    target_member: Optional[str],
    target_pool: Optional[str],
    settings: Any,
    mappings: Optional[dict[str, Any]] = None,
) -> PreflightResult:
    """Build one deterministic preflight plan without mutating either daemon."""
    source_identity = daemon_identity(source_client)
    target_identity = daemon_identity(target_client)
    same_cluster = source_cluster == target_cluster
    same_project = same_cluster and source_project == target_project
    member_relocation = intent == "move" and same_project and bool(target_member)
    strategy = (
        "member_relocation"
        if member_relocation
        else "same_cluster_copy"
        if same_cluster
        else "cross_cluster_pull"
    )
    result = PreflightResult(
        strategy=strategy,
        downtime="none" if intent == "copy" else "cold",
        capabilities={
            "source": source_identity,
            "target": target_identity,
            "same_cluster": same_cluster,
            "same_project": same_project,
            "target_member": target_member,
            "checkpoint": True,
            # The planners turn rollback on for a staged move once every contract is derived.
            "rollback": False,
            "live": False,
            "incremental_refresh": False,
            "target_project_exists": False,
            "target_project_create": False,
            "atomic_shadow_metadata_cutover": True,
            "project_dependencies_locked": True,
            "daemon_operation_inventory_verified": True,
            "daemon_operations_quiescent": True,
            "stopped_source_required": True,
            "automatic_cleanup": bool(
                getattr(settings, "migration_auto_cleanup_enabled", False)
            ),
        },
    )
    if scope == "project":
        _check_active_operations(
            result,
            source_client=source_client,
            target_client=target_client,
            source_cluster=source_cluster,
            source_project=source_project,
            target_cluster=target_cluster,
            target_project=target_project,
        )
    mapping_groups = mappings or {}
    if scope == "project" and intent == "copy":
        result.block(
            "project_copy_unsupported",
            "Project copy is not implemented by the durable relocation executor.",
            "Use project move after reviewing cutover and retention, or copy instances individually.",
        )
    unsupported_groups = (
        ("volumes", "members", "external_dependencies")
        if scope == "instance"
        else ("profiles", "volumes", "members", "external_dependencies")
    )
    for group in unsupported_groups:
        if mapping_groups.get(group):
            result.block(
                f"unsupported_{group}_mapping",
                f"{group.replace('_', ' ').title()} mappings are not applied by this executor.",
                "Remove the mapping and resolve the dependency explicitly before migration.",
            )
    unsupported_pool_keys = sorted(
        key
        for key in (mapping_groups.get("storage_pools") or {})
        if key not in {"root", "*"}
    )
    if unsupported_pool_keys:
        result.block(
            "unsupported_storage_mapping",
            "Only the root storage mapping is applied by this executor.",
            "Use storage_pools.root or storage_pools.* only.",
        )
    if source_identity["engine"] != target_identity["engine"]:
        result.block(
            "cross_engine_not_supported",
            "Source and target use different daemon engines.",
            "Use LXD-to-LXD or Incus-to-Incus for this release.",
        )
    if not same_cluster:
        try:
            registered_source = urlparse(api_endpoint(source_client))
            if registered_source.scheme != "https" or not registered_source.hostname:
                raise MigrationSecurityError(
                    "Cross-cluster migration requires a registered HTTPS endpoint."
                )
        except MigrationSecurityError:
            result.block(
                "registered_migration_endpoint_required",
                "The source cluster lacks an eligible registered HTTPS migration endpoint.",
                "Register and certificate-pin the source API endpoint before migration.",
            )
        else:
            result.warn(
                "target_to_source_reachability_unverified",
                "Cross-cluster pull uses the registered, certificate-pinned source endpoint, but target-to-source reachability cannot be pre-probed.",
                "Confirm routing and firewall access from the target cluster to the registered source endpoint.",
            )
    if same_cluster and source_project != target_project and (
        "container_copy_project" not in source_identity["extensions"]
    ):
        result.block(
            "container_copy_project_required",
            "The cluster lacks the extension required for cross-project copy.",
        )
    _apply_lxd_native_cross_project_move_gate(
        result,
        intent=intent,
        same_cluster=same_cluster,
        source_project=source_project,
        target_project=target_project,
        source_identity=source_identity,
        settings=settings,
    )
    if same_cluster and target_member and "cluster_internal_copy" not in source_identity["extensions"]:
        result.block(
            "cluster_internal_copy_required",
            "The cluster lacks the extension required for cross-member relocation.",
        )
    for side, identity in (("source", source_identity), ("target", target_identity)):
        verdict = build_verdict(identity, settings)
        if not approved_build(identity, settings):
            result.block(
                f"{side}_build_not_approved",
                f"The {side} daemon build is not admitted for migration: {verdict.reason}",
                "Upgrade the daemon to a patched build, or list its exact "
                f"{identity['engine']}:{identity['version']} token in MIGRATION_BUILD_ALLOW "
                "after a security review.",
            )
    try:
        source_project_record = _get_project(source_client, source_project)
    except InventoryReadError as exc:
        _block_inventory_error(result, side="source", error=exc)
        return result
    try:
        target_project_record = _get_project(target_client, target_project)
    except InventoryReadError as exc:
        _block_inventory_error(result, side="target", error=exc)
        return result
    result.capabilities["target_project_exists"] = target_project_record is not None
    result.capabilities["source_project_fingerprint"] = _project_fingerprint(
        source_project_record
    )
    result.capabilities["target_project_fingerprint"] = _project_fingerprint(
        target_project_record
    )
    effective_target_config = (
        (target_project_record.get("config") or {})
        if target_project_record is not None
        else (
            (source_project_record.get("config") or {})
            if source_project_record is not None
            else {}
        )
    )
    if (
        strategy == "cross_cluster_pull"
        and target_identity["engine"] == "incus"
        and str(effective_target_config.get("restricted") or "").lower() == "true"
    ):
        result.block(
            "restricted_incus_pull_target_unsupported",
            "Cross-cluster pull migration into a restricted Incus project is not supported.",
            "Use an unrestricted compatible target project until a pinned push transport is "
            "implemented and approved.",
        )
    if scope == "instance":
        from .preflight_instance import inspect_instance

        return inspect_instance(
            result,
            intent=intent,
            source_client=source_client,
            target_client=target_client,
            source_cluster=source_cluster,
            source_project=source_project,
            source_name=source_name,
            target_cluster=target_cluster,
            target_project=target_project,
            target_name=target_name,
            target_member=target_member,
            target_pool=target_pool,
            settings=settings,
            mappings=mapping_groups,
            source_identity=source_identity,
            target_identity=target_identity,
            source_project_record=source_project_record,
            target_project_record=target_project_record,
        )
    from .preflight_project import inspect_project

    return inspect_project(
        result,
        intent=intent,
        source_client=source_client,
        target_client=target_client,
        source_cluster=source_cluster,
        source_project=source_project,
        target_cluster=target_cluster,
        target_project=target_project,
        target_member=target_member,
        target_pool=target_pool,
        settings=settings,
        mappings=mapping_groups,
        target_identity=target_identity,
        source_project_record=source_project_record,
        target_project_record=target_project_record,
    )
