"""Authoritative, read-only preflight for dashboard instance creation."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from app.core.blocking import run_blocking
from app.core.lxd_path import safe_segment
from app.core.units import format_size
from app.models.schemas import (
    InstanceCreateEffectiveSummary,
    InstanceCreatePreflightIssue,
    InstanceCreatePreflightResponse,
    InstanceCreateRequest,
)
from app.services.lxd.cpu_allocation import parse_cpu_limit
from app.services.lxd.exceptions import LXDConnectionError
from app.services.lxd.nic_preflight import (
    NicPreflightError,
    collect_nics,
    merge_effective_devices,
    validate_nics,
)
from app.services.lxd.project_scope import (
    DEFAULT_PROJECT,
    effective_project,
    list_in_project,
    list_project_configs,
)
from app.services.lxd.project_usage import parse_lxd_size, read_project_usage
from app.services.lxd.utils import image_source_key


class CreatePreflightUnavailable(RuntimeError):
    """The daemon could not provide enough state for a safe create verdict."""


@dataclass(frozen=True)
class CreatePreflightEvaluation:
    """Public verdict plus sensitive effective maps for server-side policy checks only."""

    response: InstanceCreatePreflightResponse
    effective_config: Dict[str, Any]
    effective_devices: Dict[str, Dict[str, Any]]
    target_project_config: Dict[str, str] = field(default_factory=dict)


_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="lxd_create_preflight")
_MAX_IN_FLIGHT = 8
_MAX_IN_FLIGHT_PER_CLUSTER = 2
_WALL_TIMEOUT_SECONDS = 45.0
_READ_TIMEOUT = (3, 12)


def _quota_amount(quota_field: str, amount: int) -> str:
    """A quota figure as a person reads it; memory and disk figures are bytes on the wire."""
    if quota_field == "cpu":
        return f"{amount} vCPU" if amount == 1 else f"{amount} vCPUs"
    return format_size(amount)


def _quota_refusal(quota_field: str, what: str, projected: int, limit: int) -> str:
    return (
        f"Creating this instance would allocate {_quota_amount(quota_field, projected)}{what}, "
        f"over the project's limit of {_quota_amount(quota_field, limit)}."
    )


def _ordered_unique(values: Optional[Iterable[str]], default: str) -> List[str]:
    return list(dict.fromkeys(values or [default]))


def _root_device_name(devices: Dict[str, Dict[str, Any]]) -> Optional[str]:
    roots = sorted(
        name for name, device in devices.items() if device.get("type") == "disk" and device.get("path") == "/"
    )
    return roots[0] if len(roots) == 1 else None


def _root_device(devices: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    name = _root_device_name(devices)
    return devices.get(name) if name is not None else None


def _status_of(exc: Exception) -> Optional[int]:
    return getattr(getattr(exc, "response", None), "status_code", None)


def _metadata(response: Any) -> Any:
    try:
        body = response.json()
    except Exception as exc:  # noqa: BLE001 - a malformed daemon response is unavailable
        raise CreatePreflightUnavailable(
            "The daemon returned an unreadable response during instance preflight"
        ) from exc
    if not isinstance(body, dict):
        raise CreatePreflightUnavailable("The daemon returned an unreadable response during instance preflight")
    return body.get("metadata")


def _alias_names(row: Dict[str, Any]) -> List[str]:
    aliases: List[str] = []
    for alias in row.get("aliases") or []:
        if isinstance(alias, dict) and alias.get("name"):
            aliases.append(str(alias["name"]))
        elif isinstance(alias, str) and alias:
            aliases.append(alias)
    return aliases


def _matching_images(rows: List[Dict[str, Any]], reference: str) -> List[Dict[str, Any]]:
    if image_source_key(reference) == "fingerprint":
        return [row for row in rows if str(row.get("fingerprint") or "").lower().startswith(reference.lower())]
    return [row for row in rows if reference in _alias_names(row)]


def _image_state(
    client: Any,
    request: InstanceCreateRequest,
    *,
    project: str,
    project_configs: Dict[str, Dict[str, str]],
    blockers: List[InstanceCreatePreflightIssue],
) -> Dict[str, Any]:
    """Verify local image ownership without pretending a remote pull is local.

    A remote simplestreams alias is resolved by the daemon during its pull operation. The
    external catalog is neither an authoritative cluster resource nor safely probeable within
    this bounded read, so remote state is described but not reported as locally verified.
    """
    if request.source_type == "none":
        return {"source": "none"}

    reference = str(request.image or "")
    if request.image_server:
        return {
            "source": "remote",
            "server": request.image_server,
            "protocol": request.image_protocol or "simplestreams",
            "reference": reference,
            "instance_type": request.instance_type,
        }

    owner = effective_project("images", project, project_configs)
    try:
        rows = list_in_project(
            client,
            "images",
            owner,
            request_timeout=_READ_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 - image availability is required for local create
        raise CreatePreflightUnavailable(f"Could not verify image '{reference}' in image project '{owner}'") from exc

    matches = _matching_images(rows, reference)
    safe_matches = sorted(
        (
            {
                "fingerprint": str(row.get("fingerprint") or ""),
                "type": str(row.get("type") or ""),
            }
            for row in matches
        ),
        key=lambda row: (row["fingerprint"], row["type"]),
    )
    if not matches:
        blockers.append(
            InstanceCreatePreflightIssue(
                code="IMAGE_NOT_FOUND",
                field="image",
                message=(f"Image '{reference}' does not exist in the effective image project '{owner}'."),
            )
        )
    elif len(matches) > 1:
        blockers.append(
            InstanceCreatePreflightIssue(
                code="IMAGE_AMBIGUOUS",
                field="image",
                message=(
                    f"Image reference '{reference}' matches more than one image in project "
                    f"'{owner}'. Use a longer fingerprint."
                ),
            )
        )
    else:
        image_type = str(matches[0].get("type") or "")
        if not image_type:
            blockers.append(
                InstanceCreatePreflightIssue(
                    code="IMAGE_METADATA_INVALID",
                    field="image",
                    message=(f"Image '{reference}' does not report an instance type and cannot be verified safely."),
                )
            )
        elif image_type != request.instance_type:
            blockers.append(
                InstanceCreatePreflightIssue(
                    code="IMAGE_TYPE_MISMATCH",
                    field="image",
                    message=(f"Image '{reference}' is for '{image_type}', not '{request.instance_type}'."),
                )
            )

    return {
        "source": "local",
        "owner": owner,
        "reference": reference,
        "matches": safe_matches,
    }


def _storage_pool_state(
    client: Any,
    effective_devices: Dict[str, Dict[str, Any]],
    blockers: List[InstanceCreatePreflightIssue],
) -> Dict[str, Any]:
    """Verify a declared root pool. An omitted pool remains daemon-selected."""
    root = _root_device(effective_devices)
    pool = str((root or {}).get("pool") or "").strip()
    if not pool:
        return {"name": None, "selection": "daemon"}

    try:
        response = client.api.storage_pools[safe_segment(pool)].get(timeout=_READ_TIMEOUT)
        metadata = _metadata(response)
    except CreatePreflightUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - separate a true miss from an unreadable daemon
        if _status_of(exc) == 404 or "not found" in str(exc).lower():
            blockers.append(
                InstanceCreatePreflightIssue(
                    code="STORAGE_POOL_NOT_FOUND",
                    field="devices",
                    message=f"Storage pool '{pool}' does not exist on the target cluster.",
                )
            )
            return {"name": pool, "status": "missing"}
        raise CreatePreflightUnavailable(
            f"Could not verify storage pool '{pool}' before creating the instance"
        ) from exc

    if not isinstance(metadata, dict):
        raise CreatePreflightUnavailable(f"Storage pool '{pool}' returned no readable metadata")
    reported_name = str(metadata.get("name") or pool)
    if reported_name != pool:
        raise CreatePreflightUnavailable(f"Storage pool '{pool}' returned metadata for a different pool")
    status = str(metadata.get("status") or "").strip()
    if not status:
        raise CreatePreflightUnavailable(f"Storage pool '{pool}' did not report an availability status")
    if status.lower() != "created":
        blockers.append(
            InstanceCreatePreflightIssue(
                code="STORAGE_POOL_UNAVAILABLE",
                field="devices",
                message=f"Storage pool '{pool}' is not ready (status: {status}).",
            )
        )
    return {
        "name": pool,
        "status": status,
        "driver": str(metadata.get("driver") or ""),
    }


def _quota_state(
    client: Any,
    request: InstanceCreateRequest,
    effective_config: Dict[str, Any],
    effective_devices: Dict[str, Dict[str, Any]],
    *,
    project: str,
    blockers: List[InstanceCreatePreflightIssue],
) -> Dict[str, Any]:
    """Check configured aggregate quotas against authoritative current allocation."""
    try:
        usage = read_project_usage(client, project)
    except Exception as exc:  # noqa: BLE001 - project configuration itself is authoritative
        raise CreatePreflightUnavailable(f"Could not read resource limits for project '{project}'") from exc

    root = _root_device(effective_devices) or {}
    requested = {
        "cpu": parse_cpu_limit(effective_config.get("limits.cpu"), default=0),
        "memory_bytes": parse_lxd_size(effective_config.get("limits.memory")),
        "disk_bytes": parse_lxd_size(root.get("size")),
    }
    fields = {
        "cpu": ("config", "vCPU", ""),
        "memory_bytes": ("config", "memory", " of memory"),
        "disk_bytes": ("devices", "root disk", " of root disk"),
    }
    for quota_field, (request_field, label, what) in fields.items():
        limit = usage.limits.get(quota_field)
        if limit is None:
            continue
        value = requested[quota_field]
        if value is None or value <= 0:
            blockers.append(
                InstanceCreatePreflightIssue(
                    code="PROJECT_QUOTA_VALUE_REQUIRED",
                    field=request_field,
                    message=(
                        f"Project '{project}' has an aggregate {label} quota, so the create "
                        f"must provide a positive effective {label} value."
                    ),
                )
            )
            continue
        if not usage.is_complete(quota_field):
            blockers.append(
                InstanceCreatePreflightIssue(
                    code="QUOTA_USAGE_UNAVAILABLE",
                    field=request_field,
                    message=(
                        f"Current {label} allocation for project '{project}' could not be "
                        "read completely, so the quota cannot be checked safely."
                    ),
                )
            )
            continue
        projected = usage.used.get(quota_field, 0) + value
        if projected > limit:
            blockers.append(
                InstanceCreatePreflightIssue(
                    code="PROJECT_QUOTA_EXCEEDED",
                    field=request_field,
                    message=_quota_refusal(quota_field, what, projected, limit),
                )
            )

    relevant = ("cpu", "memory_bytes", "disk_bytes")
    return {
        "limits": {field: usage.limits.get(field) for field in relevant},
        "used": {field: usage.used.get(field, 0) for field in relevant},
        "complete": {field: usage.is_complete(field) for field in relevant},
        "requested": requested,
        "instance_type": request.instance_type,
    }


def _revision(
    request: InstanceCreateRequest,
    *,
    profile_owner: str,
    profile_configs: List[Dict[str, Any]],
    profile_devices: List[Dict[str, Any]],
    network_owner: str,
    network_rows: List[Dict[str, Any]],
    duplicate: bool,
    image_state: Dict[str, Any],
    storage_pool_state: Dict[str, Any],
    quota_state: Dict[str, Any],
    project_config: Dict[str, str],
) -> str:
    request_state = request.model_dump(
        mode="json",
        exclude={"acknowledgements", "plan_revision", "cloud_init"},
    )
    # Cloud-init can contain credentials. Only its one-way digest participates in the plan,
    # so neither the response nor logs can disclose its contents.
    request_state["cloud_init_sha256"] = hashlib.sha256((request.cloud_init or "").encode("utf-8")).hexdigest()
    relevant_networks = [
        {
            "name": row.get("name"),
            "managed": bool(row.get("managed")),
            "ipv4.address": (row.get("config") or {}).get("ipv4.address"),
        }
        for row in network_rows
    ]
    material = {
        "request": request_state,
        "profile_owner": profile_owner,
        "profile_configs": profile_configs,
        "profile_devices": profile_devices,
        "network_owner": network_owner,
        "networks": sorted(relevant_networks, key=lambda row: str(row.get("name") or "")),
        "duplicate": duplicate,
        "image": image_state,
        "storage_pool": storage_pool_state,
        "quota": quota_state,
        "project_config": project_config,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _inspect_create(
    lxd_service: Any,
    request: InstanceCreateRequest,
) -> CreatePreflightEvaluation:
    """Read the exact project state and produce a deterministic preflight verdict."""
    try:
        client = lxd_service.get_client_with_reconnect(request.host)
    except Exception as exc:  # noqa: BLE001 - converted to a safe availability result
        raise CreatePreflightUnavailable(f"Could not connect to {request.host} for instance preflight") from exc
    if client is None:
        raise LXDConnectionError(f"Could not connect to {request.host}")

    project = request.project or DEFAULT_PROJECT
    profiles = _ordered_unique(request.profiles, "default")
    try:
        # The target project's exact config is also an input to shared-cluster isolation policy.
        # Read it even for ``default``; assuming an empty config here could admit a Partner write
        # against an unrestricted default project.
        project_configs = list_project_configs(client)
        profile_owner = effective_project("profiles", project, project_configs)
        profile_rows = list_in_project(
            client,
            "profiles",
            profile_owner,
            request_timeout=_READ_TIMEOUT,
        )
        profile_by_name = {str(row.get("name")): row for row in profile_rows if row.get("name")}
        ordered_profile_devices = [
            dict(profile_by_name[name].get("devices") or {}) for name in profiles if name in profile_by_name
        ]
        ordered_profile_configs = [
            dict(profile_by_name[name].get("config") or {}) for name in profiles if name in profile_by_name
        ]
        effective_devices = merge_effective_devices(request.devices, ordered_profile_devices)
        effective_config: Dict[str, Any] = {}
        for profile_config in ordered_profile_configs:
            effective_config.update(profile_config)
        effective_config.update(request.config or {})
        if request.cloud_init:
            effective_config["user.user-data"] = request.cloud_init
        nics = collect_nics(request.devices, ordered_profile_devices)

        network_owner = effective_project("networks", project, project_configs)
        network_rows = list_in_project(
            client,
            "networks",
            network_owner,
            request_timeout=_READ_TIMEOUT,
        )
        instance_rows = list_in_project(
            client,
            "instances",
            project,
            request_timeout=_READ_TIMEOUT,
        )
    except (LXDConnectionError, CreatePreflightUnavailable):
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed when authoritative reads fail
        raise CreatePreflightUnavailable(f"Could not verify project '{project}' before creating the instance") from exc

    duplicate = any(row.get("name") == request.name for row in instance_rows)
    blockers: List[InstanceCreatePreflightIssue] = []
    missing_profiles = [name for name in profiles if name not in profile_by_name]
    if missing_profiles:
        blockers.append(
            InstanceCreatePreflightIssue(
                code="PROFILE_NOT_FOUND",
                field="profiles",
                message=(
                    "The following profiles do not exist in the effective profile project "
                    f"'{profile_owner}': {', '.join(missing_profiles)}."
                ),
            )
        )
    if duplicate:
        blockers.append(
            InstanceCreatePreflightIssue(
                code="INSTANCE_ALREADY_EXISTS",
                field="name",
                message=(
                    f"An instance named '{request.name}' already exists in project '{project}' on {request.host}."
                ),
            )
        )

    root_names = sorted(
        name for name, device in effective_devices.items() if device.get("type") == "disk" and device.get("path") == "/"
    )
    if len(root_names) != 1:
        blockers.append(
            InstanceCreatePreflightIssue(
                code="ROOT_DISK_INVALID",
                field="devices",
                message=("The effective device set must contain exactly one root disk with path '/'."),
            )
        )

    try:
        validate_nics(project, nics, network_rows)
    except NicPreflightError as exc:
        blockers.append(
            InstanceCreatePreflightIssue(
                code="NETWORK_INVALID",
                field="devices",
                message=str(exc),
            )
        )

    image_state = _image_state(
        client,
        request,
        project=project,
        project_configs=project_configs,
        blockers=blockers,
    )
    storage_pool_state = _storage_pool_state(client, effective_devices, blockers)
    quota_state = _quota_state(
        client,
        request,
        effective_config,
        effective_devices,
        project=project,
        blockers=blockers,
    )

    warnings: List[InstanceCreatePreflightIssue] = []
    required_acknowledgements: List[str] = []
    if not nics:
        required_acknowledgements.append("networkless")
        warnings.append(
            InstanceCreatePreflightIssue(
                code="NETWORKLESS",
                field="devices",
                message=(
                    "No network interface remains after applying profiles and instance "
                    "overrides. The instance will be isolated from the network."
                ),
                acknowledgement_required=True,
            )
        )

    revision = _revision(
        request,
        profile_owner=profile_owner,
        profile_configs=ordered_profile_configs,
        profile_devices=ordered_profile_devices,
        network_owner=network_owner,
        network_rows=network_rows,
        duplicate=duplicate,
        image_state=image_state,
        storage_pool_state=storage_pool_state,
        quota_state=quota_state,
        project_config=dict(project_configs.get(project) or {}),
    )
    acknowledged = set(request.acknowledgements)
    return CreatePreflightEvaluation(
        response=InstanceCreatePreflightResponse(
            ready=not blockers and set(required_acknowledgements).issubset(acknowledged),
            plan_revision=revision,
            blockers=blockers,
            warnings=warnings,
            required_acknowledgements=required_acknowledgements,
            effective=InstanceCreateEffectiveSummary(
                profiles=profiles,
                nic_names=[name for name, _device in nics],
                root_device=_root_device_name(effective_devices),
            ),
        ),
        effective_config=effective_config,
        effective_devices=effective_devices,
        target_project_config=dict(project_configs.get(project) or {}),
    )


async def evaluate_instance_create(
    lxd_service: Any,
    request: InstanceCreateRequest,
) -> CreatePreflightEvaluation:
    """Run create preflight off the event loop with a bounded pool and wall budget."""
    return await run_blocking(
        _inspect_create,
        lxd_service,
        request,
        timeout=_WALL_TIMEOUT_SECONDS,
        label=f"preflight create {request.host}/{request.project}/{request.name}",
        executor=_executor,
        max_in_flight=_MAX_IN_FLIGHT,
        concurrency_key=request.host,
        max_in_flight_per_key=_MAX_IN_FLIGHT_PER_CLUSTER,
    )


async def preflight_instance_create(
    lxd_service: Any,
    request: InstanceCreateRequest,
) -> InstanceCreatePreflightResponse:
    """Run the preflight and return only the public response."""
    return (await evaluate_instance_create(lxd_service, request)).response
