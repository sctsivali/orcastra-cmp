"""Typed, concurrency-safe edits to an existing instance's resource specification.

Only CPU count, absolute memory bytes and root-disk growth are accepted. The implementation
reads the exact ``(cluster, project, instance)`` record, preserves every unrelated writable
field, sends LXD's ETag back with the PUT, waits for the daemon operation, and reads the result
back before calling it successful.

The orchestration layer is durable. A request row is committed before any mutation, carries a
short database lease, and can be reconciled after a worker restart without blindly submitting
the write twice.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Mapping, Optional

import requests.exceptions as requests_exc
from pylxd import exceptions as pylxd_exc
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking
from app.core.cache import get_cache_manager
from app.core.database import async_session_maker
from app.core.error_sanitizer import sanitize_error
from app.core.lxd_path import safe_segment
from app.models.db_models import InstanceOperationJob
from app.services.instance_operation_audit import (
    drain_instance_operation_audits,
    enqueue_instance_operation_audit,
)
from app.services.lxd.project_usage import parse_lxd_size, read_project_usage
from app.services.lxd.utils import wait_for_lxd_operation, write_timeout
from app.services.lxd_service import get_lxd_service
from app.services.migration_fence import (
    MigrationFenceConflict,
    guard_instance_mutation,
)

logger = logging.getLogger(__name__)

_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="instance-spec")
_MAX_IN_FLIGHT = 8
_MAX_IN_FLIGHT_PER_CLUSTER = 2
_READ_TIMEOUT_SECONDS = 35.0
_INSTANCE_READ_REQUEST_TIMEOUT = (3, 20)
_VM_ROOT_STATE_REQUEST_TIMEOUT = (3, 8)
_WRITE_TIMEOUT_SECONDS = 90.0
_STATE_TIMEOUT_SECONDS = 75.0
_LEASE_SECONDS = 5 * 60
_LEASE_HEARTBEAT_SECONDS = 30
_VM_MEMORY_HOTPLUG_SAFE_MAX_BYTES = 1 << 40
_cluster_locks: dict[str, asyncio.Lock] = {}

ACTIVE_STATUSES = ("pending", "running", "unknown")
TERMINAL_STATUSES = ("success", "failure", "partial")
_FORBIDDEN_JOB_METADATA_KEYS = {
    "cloud_init",
    "cloud-init",
    "user_data",
    "user-data",
    "config",
    "devices",
    "password",
    "secret",
    "token",
    "private_key",
    "certificate",
}


class SpecificationError(Exception):
    """Base error with a stable API status and optional structured detail."""

    status_code = 400

    def __init__(self, message: str, *, detail: Any = None) -> None:
        super().__init__(message)
        self.detail = detail if detail is not None else message


class SpecificationNotFound(SpecificationError):
    status_code = 404


class SpecificationConflict(SpecificationError):
    status_code = 409


class SpecificationStale(SpecificationError):
    status_code = 412


class SpecificationUnavailable(SpecificationError):
    status_code = 503


class SpecificationTimeout(SpecificationError):
    status_code = 504


class SpecificationWriteUnknown(Exception):
    """The daemon may have accepted a write, but its terminal verdict was not observed."""

    def __init__(self, operation_id: Optional[str], cause: BaseException) -> None:
        self.operation_id = operation_id
        self.cause = cause
        super().__init__(str(cause))


@dataclass(frozen=True)
class SpecificationRead:
    public: Dict[str, Any]
    raw: Dict[str, Any]
    etag: Optional[str]
    root_device_name: Optional[str]


def _params(project: str) -> Dict[str, str]:
    # Always name the project, including default. A missing param must never mean "search every
    # project" on a write path where same-named instances may exist in two organizations.
    return {"project": project}


def _etag_value(value: Optional[str]) -> Optional[str]:
    text = (value or "").strip()
    return text or None


def etags_match(sent: str, current: Optional[str]) -> bool:
    """Compare HTTP ETags without treating their quotes as part of the version."""
    if not current:
        return False
    return sent.strip().removeprefix("W/").strip('"') == current.strip().removeprefix("W/").strip('"')


def _source(local: Mapping[str, Any], expanded: Mapping[str, Any], key: str) -> str:
    if key in local:
        return "instance"
    if key in expanded:
        return "profile"
    return "default"


def _cpu_value(value: Any) -> Optional[int]:
    """Return the number of CPUs represented by a count or a valid pin set."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        result = int(text)
        return result if result > 0 else None
    ids: set[int] = set()
    for part in text.split(","):
        low, sep, high = part.strip().partition("-")
        if not sep:
            if not low.isdigit():
                return None
            ids.add(int(low))
            continue
        if not low.isdigit() or not high.isdigit() or int(high) < int(low):
            return None
        ids.update(range(int(low), int(high) + 1))
    return len(ids) or None


def _root_device(data: Mapping[str, Any]) -> tuple[Optional[str], Dict[str, Any], str]:
    local = data.get("devices") or {}
    expanded = data.get("expanded_devices") or {}
    for name, device in local.items():
        if isinstance(device, dict) and device.get("type") == "disk" and device.get("path") == "/":
            effective = expanded.get(name) if isinstance(expanded.get(name), dict) else device
            return name, dict(effective), "instance"
    for name, device in expanded.items():
        if isinstance(device, dict) and device.get("type") == "disk" and device.get("path") == "/":
            return name, dict(device), "profile"
    return None, {}, "default"


def _vm_effective_root_size(
    client: Any,
    *,
    instance_name: str,
    project: str,
    pool: Optional[str],
) -> Optional[int]:
    """Read the daemon's effective VM block allocation when no size is configured.

    VM root volumes are block devices and Incus rejects block shrink through ordinary quota
    updates. Container filesystems do not share that invariant, so this fallback must never be
    used for a container.
    """
    if not pool:
        return None
    try:
        volume = client.api.storage_pools[safe_segment(pool)].volumes[
            safe_segment("virtual-machine")
        ][safe_segment(instance_name)]
        state_response = volume.state.get(
            params=_params(project),
            timeout=_VM_ROOT_STATE_REQUEST_TIMEOUT,
        )
        state_body = state_response.json()
        if not isinstance(state_body, Mapping):
            return None
        state_metadata = state_body.get("metadata")
        if not isinstance(state_metadata, Mapping):
            return None
        usage = state_metadata.get("usage")
        if not isinstance(usage, Mapping):
            return None
        raw_total = usage.get("total")
        if isinstance(raw_total, bool) or not isinstance(raw_total, (int, str)):
            return None
        try:
            total = int(raw_total.strip()) if isinstance(raw_total, str) else raw_total
        except ValueError:
            return None
        return total if total > 0 else None
    except Exception as exc:  # noqa: BLE001 - absence means editing is disabled, not guessed
        logger.info(
            "Could not resolve the effective VM root size for %s/%s: %s",
            project,
            instance_name,
            sanitize_error(str(exc)),
        )
        return None


def read_specification_sync(
    client: Any, instance_name: str, host: str, project: str
) -> SpecificationRead:
    """Read one exact instance and project as a safe specification projection."""
    try:
        response = client.api.instances[safe_segment(instance_name)].get(
            params=_params(project),
            timeout=_INSTANCE_READ_REQUEST_TIMEOUT,
        )
    except pylxd_exc.NotFound:
        raise SpecificationNotFound(f"Instance '{instance_name}' was not found") from None
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 404 or "not found" in str(exc).lower():
            raise SpecificationNotFound(f"Instance '{instance_name}' was not found") from None
        raise

    data = response.json().get("metadata") or {}
    local_config = data.get("config") or {}
    expanded_config = data.get("expanded_config") or local_config
    instance_type = data.get("type") or "container"
    root_name, root, root_source = _root_device(data)
    configured_root_size = parse_lxd_size(root.get("size"))
    if configured_root_size is not None and configured_root_size <= 0:
        configured_root_size = None
    root_size = configured_root_size
    if root_size is None and instance_type == "virtual-machine":
        root_size = _vm_effective_root_size(
            client,
            instance_name=instance_name,
            project=project,
            pool=root.get("pool"),
        )

    # A running container can accept a logical size decrease while Incus defers the physical
    # quota change until its next start. Treat that marker as an unknown backing-size boundary;
    # replacing it with a value between the logical and physical sizes could perform a shrink.
    pending_quota_change = bool(
        root_name
        and str(local_config.get(f"volatile.{root_name}.apply_quota") or "").strip()
    )

    etag = _etag_value(getattr(response, "headers", {}).get("ETag"))
    status = data.get("status") or "Unknown"
    if not root_name:
        root_reason = "The instance has no effective root disk."
    elif pending_quota_change:
        root_reason = (
            "Incus has a deferred root-disk quota change pending. Resolve and verify it "
            "directly in Incus before requesting another resize; Orcastra will not start "
            "the instance automatically."
        )
    elif root_size is None:
        root_reason = "The current root-disk allocation could not be read safely."
    elif not etag:
        root_reason = "The instance version could not be read safely."
    else:
        root_reason = None
    public: Dict[str, Any] = {
        "instance_name": instance_name,
        "host": host,
        "project": project,
        "type": instance_type,
        "status": status,
        "cpu": {
            "value": _cpu_value(expanded_config.get("limits.cpu")),
            "source": _source(local_config, expanded_config, "limits.cpu"),
        },
        "memory": {
            "value_bytes": parse_lxd_size(expanded_config.get("limits.memory")),
            "source": _source(local_config, expanded_config, "limits.memory"),
        },
        "root_disk": {
            "value_bytes": root_size,
            "pool": root.get("pool"),
            "source": root_source,
            "editable": bool(
                root_name and root_size is not None and etag and not pending_quota_change
            ),
            "pending_quota_change": pending_quota_change,
            "reason": root_reason,
        },
        "capabilities": {
            # CPU and ordinary memory-limit writes are live-capable for both instance types.
            # Preflight narrows this per requested field (root disk and an explicitly disabled
            # VM memory-hotplug path can still require a restart).
            "live_update": True,
            "restart_supported": status in {"Running", "Stopped"},
        },
        "etag": etag,
    }
    return SpecificationRead(public=public, raw=dict(data), etag=etag, root_device_name=root_name)


def _change(field: str, before: Optional[int], after: int, source: str) -> Dict[str, Any]:
    return {"field": field, "before": before, "after": after, "source": source}


def _message(code: str, message: str, *, field: Optional[str] = None) -> Dict[str, Any]:
    value: Dict[str, Any] = {"code": code, "message": message}
    if field:
        value["field"] = field
    return value


def _warning(code: str, message: str, *, acknowledgement: Optional[str] = None) -> Dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "requires_acknowledgement": acknowledgement is not None,
    }


def _plan_revision(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_preflight(
    current: SpecificationRead,
    requested: Mapping[str, Optional[int]],
    *,
    project_usage: Any = None,
) -> Dict[str, Any]:
    """Build a deterministic plan from a fresh read and optional aggregate project usage."""
    cpu = requested.get("cpu_count")
    memory = requested.get("memory_bytes")
    root = requested.get("root_disk_bytes")
    changes: list[Dict[str, Any]] = []
    warnings: list[Dict[str, Any]] = []
    blockers: list[Dict[str, Any]] = []
    acknowledgements: list[str] = []

    if not current.etag:
        blockers.append(
            _message(
                "ETAG_UNAVAILABLE",
                "The cluster did not return an instance version, so a safe concurrent update is unavailable.",
            )
        )

    current_cpu = current.public["cpu"]["value"]
    current_memory = current.public["memory"]["value_bytes"]
    current_root = current.public["root_disk"]["value_bytes"]
    if cpu is not None and cpu != current_cpu:
        changes.append(_change("cpu_count", current_cpu, cpu, current.public["cpu"]["source"]))
    if memory is not None and memory != current_memory:
        changes.append(
            _change("memory_bytes", current_memory, memory, current.public["memory"]["source"])
        )
        if current_memory is not None and memory < current_memory:
            acknowledgements.append("memory_decrease")
            warnings.append(
                _warning(
                    "MEMORY_DECREASE",
                    "Reducing memory can pressure or terminate workload processes.",
                    acknowledgement="memory_decrease",
                )
            )
    if root is not None:
        if current.public["root_disk"].get("pending_quota_change"):
            blockers.append(
                _message(
                    "ROOT_DISK_QUOTA_PENDING",
                    current.public["root_disk"]["reason"],
                    field="root_disk_bytes",
                )
            )
        elif current_root is None or not current.public["root_disk"]["editable"]:
            blockers.append(
                _message(
                    "ROOT_SIZE_UNAVAILABLE",
                    "The current root-disk allocation cannot be verified, so it is not safe to resize it.",
                    field="root_disk_bytes",
                )
            )
        elif root < current_root:
            blockers.append(
                _message(
                    "ROOT_DISK_SHRINK_UNSUPPORTED",
                    "Root disks can only be enlarged; shrinking is not supported.",
                    field="root_disk_bytes",
                )
            )
        elif root > current_root:
            changes.append(
                _change(
                    "root_disk_bytes", current_root, root, current.public["root_disk"]["source"]
                )
            )
            acknowledgements.append("root_disk_growth")
            warnings.append(
                _warning(
                    "ROOT_DISK_GROWTH",
                    "Root-disk growth is irreversible from this console.",
                    acknowledgement="root_disk_growth",
                )
            )

    if not changes and not blockers:
        blockers.append(_message("NO_CHANGES", "The requested values already match the instance."))

    status = current.public["status"]
    changed_fields = {item["field"] for item in changes}
    vm_running = status == "Running" and current.public["type"] == "virtual-machine"
    expanded_config = current.raw.get("expanded_config") or current.raw.get("config") or {}
    current_cpu_setting = str(expanded_config.get("limits.cpu") or "").strip()
    current_cpu_is_count = current_cpu_setting.isdigit() and int(current_cpu_setting) > 0
    cpu_restart_needed = bool(
        vm_running
        and "cpu_count" in changed_fields
        and (
            not current_cpu_is_count
            # Incus can hotplug at most 64 VM vCPUs. A larger count is valid, but only while
            # stopped. Keeping the typed API's upper bound larger preserves that supported path.
            or (cpu is not None and cpu > 64)
        )
    )
    memory_increase = (
        "memory_bytes" in changed_fields
        and current_memory is not None
        and memory is not None
        and memory > current_memory
    )
    memory_hotplug_disabled = str(
        expanded_config.get("limits.memory.hotplug", "")
    ).strip().lower() in {"false", "0", "no", "off"}
    configured_hotplug_limit = parse_lxd_size(
        expanded_config.get("limits.memory.hotplug")
    )
    memory_hotplug_limit_exceeded = bool(
        memory_increase
        and memory is not None
        and (
            (
                configured_hotplug_limit is not None
                and memory > configured_hotplug_limit
            )
            # Incus documents 1 TiB as the cluster hotplug ceiling. Standalone limits can be
            # lower depending on host/CPU address bits, so values beyond the documented common
            # ceiling are never advertised as live-capable without a daemon-specific probe.
            or memory > _VM_MEMORY_HOTPLUG_SAFE_MAX_BYTES
        )
    )
    root_restart_needed = status == "Running" and "root_disk_bytes" in changed_fields
    restart_needed = root_restart_needed or (
        vm_running
        and (
            cpu_restart_needed
            or (
                memory_increase
                and (memory_hotplug_disabled or memory_hotplug_limit_exceeded)
            )
        )
    )

    if status == "Stopped":
        execution = "stopped"
    elif restart_needed:
        execution = "restart_required"
        acknowledgements.append("restart_instance")
        warnings.append(
            _warning(
                "RESTART_REQUIRED",
                "This instance must be gracefully stopped and started for the requested field change.",
                acknowledgement="restart_instance",
            )
        )
    elif status == "Running":
        execution = "live"
        if vm_running and changed_fields.intersection({"cpu_count", "memory_bytes"}):
            warnings.append(
                _warning(
                    "VM_GUEST_ACTION_MAY_BE_REQUIRED",
                    "The limit is live-updatable, but the guest may need hotplug support or an in-guest action before it observes the full change.",
                )
            )
    else:
        execution = "stopped"
        blockers.append(
            _message(
                "INSTANCE_STATE_UNSUPPORTED",
                f"Specification changes are unavailable while the instance is {status}.",
            )
        )

    if (
        execution == "restart_required"
        and current.public["root_disk"].get("pending_quota_change")
        and not any(item["code"] == "ROOT_DISK_QUOTA_PENDING" for item in blockers)
    ):
        blockers.append(
            _message(
                "ROOT_DISK_QUOTA_PENDING",
                (
                    "Incus has a deferred root-disk quota change pending, so an automatic "
                    "restart is unsafe until that change has been verified."
                ),
                field="root_disk_bytes",
            )
        )

    if project_usage is not None:
        limits = project_usage.limits
        used = project_usage.used
        quota_fields = (
            ("cpu_count", "cpu", current_cpu),
            ("memory_bytes", "memory_bytes", current_memory),
            ("root_disk_bytes", "disk_bytes", current_root),
        )
        for request_field, quota_field, old_value in quota_fields:
            new_value = requested.get(request_field)
            limit = limits.get(quota_field)
            if new_value is None or limit is None:
                continue
            if not project_usage.is_complete(quota_field):
                blockers.append(
                    _message(
                        "QUOTA_USAGE_UNAVAILABLE",
                        "Project usage could not be read completely, so its limit cannot be checked safely.",
                        field=request_field,
                    )
                )
                continue
            projected = used.get(quota_field, 0) - (old_value or 0) + new_value
            if projected > limit:
                blockers.append(
                    _message(
                        "PROJECT_QUOTA_EXCEEDED",
                        f"The requested value would use {projected} of the project's {limit} limit.",
                        field=request_field,
                    )
                )

    required = sorted(set(acknowledgements))
    plan_basis = {
        "target": [current.public["host"], current.public["project"], current.public["instance_name"]],
        "etag": current.etag,
        "requested": {key: requested.get(key) for key in sorted(requested)},
        "execution": execution,
        "changes": changes,
        "warnings": warnings,
        "blockers": blockers,
    }
    return {
        "instance_name": current.public["instance_name"],
        "host": current.public["host"],
        "project": current.public["project"],
        "status": status,
        "type": current.public["type"],
        "etag": current.etag,
        "plan_revision": _plan_revision(plan_basis),
        "execution": execution,
        "changes": changes,
        "warnings": warnings,
        "blockers": blockers,
        "required_acknowledgements": required,
    }


def _operation_id(response: Any) -> Optional[str]:
    try:
        body = response.json()
    except Exception:
        return None
    path = body.get("operation") if isinstance(body, dict) else None
    metadata = body.get("metadata") if isinstance(body, dict) else None
    if isinstance(path, str) and path:
        return path.rstrip("/").split("/")[-1]
    if isinstance(metadata, dict):
        return metadata.get("id")
    return None


def _writable_payload(data: Mapping[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "config": dict(data.get("config") or {}),
        "devices": {key: dict(value) for key, value in (data.get("devices") or {}).items()},
        "profiles": list(data.get("profiles") or []),
        "description": data.get("description") or "",
        "ephemeral": bool(data.get("ephemeral")),
        "stateful": bool(data.get("stateful")),
    }
    if data.get("architecture"):
        payload["architecture"] = data["architecture"]
    return payload


def _matches_target(public: Mapping[str, Any], requested: Mapping[str, Any]) -> bool:
    if requested.get("root_disk_bytes") is not None:
        root_disk = public.get("root_disk") or {}
        if root_disk.get("pending_quota_change") or not root_disk.get("editable"):
            return False
    comparisons = {
        "cpu_count": (public.get("cpu") or {}).get("value"),
        "memory_bytes": (public.get("memory") or {}).get("value_bytes"),
        "root_disk_bytes": (public.get("root_disk") or {}).get("value_bytes"),
    }
    return all(value is None or comparisons[key] == value for key, value in requested.items())


def apply_specification_sync(
    client: Any,
    *,
    instance_name: str,
    host: str,
    project: str,
    expected_etag: str,
    expected_plan_revision: str,
    requested: Mapping[str, Optional[int]],
    acknowledgements: Iterable[str],
) -> tuple[SpecificationRead, Optional[str]]:
    """Apply one preflighted update and return verified daemon state plus operation id."""
    current = read_specification_sync(client, instance_name, host, project)
    if not etags_match(expected_etag, current.etag):
        raise SpecificationStale(
            "This instance changed since it was read. Reload it and review the change again."
        )
    usage = read_project_usage(client, project)
    plan = build_preflight(current, requested, project_usage=usage)
    if expected_plan_revision and plan["plan_revision"] != expected_plan_revision:
        raise SpecificationConflict(
            "The specification plan is stale. Run preflight again before applying the change."
        )
    if plan["blockers"]:
        raise SpecificationConflict("The specification change is blocked.", detail=plan)
    missing = set(plan["required_acknowledgements"]) - set(acknowledgements)
    if missing:
        raise SpecificationConflict(
            "Required warnings were not acknowledged.",
            detail={"message": "Required warnings were not acknowledged.", "missing": sorted(missing)},
        )
    if (
        requested.get("root_disk_bytes") is not None
        and current.public.get("status") != "Stopped"
    ):
        raise SpecificationConflict(
            "Root-disk growth requires a fresh Stopped readback immediately before the update."
        )

    payload = _writable_payload(current.raw)
    if requested.get("cpu_count") is not None:
        payload["config"]["limits.cpu"] = str(requested["cpu_count"])
    if requested.get("memory_bytes") is not None:
        payload["config"]["limits.memory"] = f"{requested['memory_bytes']}B"
    if requested.get("root_disk_bytes") is not None:
        if not current.root_device_name:
            raise SpecificationConflict("The instance has no effective root disk to resize.")
        own = payload["devices"].get(current.root_device_name)
        if not isinstance(own, dict):
            effective = (current.raw.get("expanded_devices") or {}).get(current.root_device_name)
            if not isinstance(effective, dict):
                raise SpecificationConflict("The effective root disk disappeared before the update.")
            own = dict(effective)
        else:
            own = dict(own)
        own["size"] = f"{requested['root_disk_bytes']}B"
        payload["devices"][current.root_device_name] = own

    try:
        operation_id: Optional[str] = None
        response = client.api.instances[safe_segment(instance_name)].put(
            json=payload,
            params=_params(project),
            headers={"If-Match": current.etag},
            timeout=write_timeout(65),
        )
        operation_id = _operation_id(response)
        try:
            wait_for_lxd_operation(client, response, timeout=60, project=project)
        except (requests_exc.Timeout, requests_exc.ConnectionError) as exc:
            raise SpecificationWriteUnknown(operation_id, exc) from exc
        except pylxd_exc.LXDAPIException as exc:
            if "still" in str(exc).lower() and "after" in str(exc).lower():
                raise SpecificationWriteUnknown(operation_id, exc) from exc
            raise
    except SpecificationWriteUnknown:
        raise
    except (requests_exc.Timeout, requests_exc.ConnectionError) as exc:
        # A transport failure while submitting the PUT does not prove the daemon rejected it.
        # Keep the target exclusive and reconcile by operation/readback instead of allowing a
        # second write to race an update that may still be applying.
        raise SpecificationWriteUnknown(operation_id, exc) from exc

    try:
        verified = read_specification_sync(client, instance_name, host, project)
    except Exception as exc:  # noqa: BLE001 - accepted write without readback is ambiguous
        raise SpecificationWriteUnknown(operation_id, exc) from exc
    if not _matches_target(verified.public, requested):
        raise SpecificationWriteUnknown(
            operation_id,
            RuntimeError("The daemon operation finished but the requested values were not observed."),
        )
    return verified, operation_id


def change_instance_state_sync(
    client: Any, instance_name: str, project: str, action: str
) -> Optional[str]:
    """Gracefully stop or start an exact instance, waiting for LXD's terminal verdict."""
    if action not in {"stop", "start"}:
        raise ValueError("Unsupported instance state action")
    payload: Dict[str, Any] = {"action": action, "force": False}
    if action == "stop":
        payload["timeout"] = 30
    try:
        response = client.api.instances[safe_segment(instance_name)].state.put(
            json=payload,
            params=_params(project),
            timeout=write_timeout(65),
        )
    except (requests_exc.Timeout, requests_exc.ConnectionError) as exc:
        # A transport timeout does not prove that the daemon rejected the action. Recovery
        # must inspect actual state before deciding whether to continue or compensate.
        raise SpecificationWriteUnknown(None, exc) from exc
    operation_id = _operation_id(response)
    try:
        wait_for_lxd_operation(client, response, timeout=60, project=project)
    except (requests_exc.Timeout, requests_exc.ConnectionError) as exc:
        raise SpecificationWriteUnknown(operation_id, exc) from exc
    except pylxd_exc.LXDAPIException as exc:
        if "still" in str(exc).lower() and "after" in str(exc).lower():
            raise SpecificationWriteUnknown(operation_id, exc) from exc
        raise
    return operation_id


def read_operation_verdict_sync(
    client: Any, operation_id: Optional[str], project: str
) -> Dict[str, Optional[str]]:
    """Return a sanitized terminal/running verdict for one daemon operation."""
    if not operation_id:
        return {"verdict": "unknown", "error": None}
    try:
        response = client.api.operations[safe_segment(operation_id)].get(
            params=_params(project)
        )
    except Exception as exc:  # noqa: BLE001 - expired/missing operations need readback fallback
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 404:
            return {"verdict": "expired", "error": None}
        raise
    metadata = response.json().get("metadata") or {}
    status_code = metadata.get("status_code")
    status_text = str(metadata.get("status") or "").strip().lower()
    error = sanitize_error(str(metadata.get("err") or "")).strip() or None
    if status_code == 200 or status_text in {"success", "successful"}:
        return {"verdict": "success", "error": None}
    if (
        isinstance(status_code, int)
        and status_code >= 400
        or status_text in {"failure", "failed", "cancelled", "canceled"}
    ):
        return {"verdict": "failure", "error": error or "The daemon operation failed."}
    return {"verdict": "running", "error": None}


async def _bounded(
    fn: Any,
    *args: Any,
    timeout: float,
    label: str,
    concurrency_key: str,
    **kwargs: Any,
) -> Any:
    try:
        return await run_blocking(
            fn,
            *args,
            timeout=timeout,
            label=label,
            executor=_EXECUTOR,
            max_in_flight=_MAX_IN_FLIGHT,
            concurrency_key=concurrency_key,
            max_in_flight_per_key=_MAX_IN_FLIGHT_PER_CLUSTER,
            **kwargs,
        )
    except BlockingPoolFull as exc:
        raise SpecificationUnavailable(
            "Instance specification operations are saturated. Try again shortly."
        ) from exc
    except BlockingTimeout as exc:
        raise SpecificationTimeout(
            "The cluster did not finish the specification operation in time."
        ) from exc


async def get_client(host: str) -> Any:
    client = await _bounded(
        get_lxd_service().get_client_with_reconnect,
        host,
        timeout=_READ_TIMEOUT_SECONDS,
        label=f"instance specification connect {host}",
        concurrency_key=host,
    )
    if client is None:
        raise SpecificationUnavailable(f"Could not connect to {host}")
    return client


async def read_specification(instance_name: str, host: str, project: str) -> SpecificationRead:
    client = await get_client(host)
    return await _bounded(
        read_specification_sync,
        client,
        instance_name,
        host,
        project,
        timeout=_READ_TIMEOUT_SECONDS,
        label=f"read instance specification {host}/{project}/{instance_name}",
        concurrency_key=host,
    )


async def preflight_specification(
    instance_name: str,
    host: str,
    project: str,
    requested: Mapping[str, Optional[int]],
) -> tuple[SpecificationRead, Dict[str, Any]]:
    client = await get_client(host)
    current = await _bounded(
        read_specification_sync,
        client,
        instance_name,
        host,
        project,
        timeout=_READ_TIMEOUT_SECONDS,
        label=f"preflight instance specification {host}/{project}/{instance_name}",
        concurrency_key=host,
    )
    usage = await _bounded(
        read_project_usage,
        client,
        project,
        timeout=_READ_TIMEOUT_SECONDS,
        label=f"read project usage {host}/{project}",
        concurrency_key=host,
    )
    return current, build_preflight(current, requested, project_usage=usage)


def safe_requested(body: Mapping[str, Any]) -> Dict[str, int]:
    return {
        key: int(value)
        for key in ("cpu_count", "memory_bytes", "root_disk_bytes")
        if (value := body.get(key)) is not None
    }


def request_hash(
    *,
    host: str,
    project: str,
    instance_name: str,
    requested: Mapping[str, int],
    plan_revision: str,
    apply_mode: str,
    acknowledgements: Iterable[str],
) -> str:
    value = {
        "host": host,
        "project": project,
        "instance_name": instance_name,
        "requested": dict(sorted(requested.items())),
        "plan_revision": plan_revision,
        "apply_mode": apply_mode,
        "acknowledgements": sorted(set(acknowledgements)),
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def job_payload(row: InstanceOperationJob) -> Dict[str, Any]:
    outcome = row.status if row.status in TERMINAL_STATUSES else None
    return {
        "job_id": row.job_id,
        "kind": row.kind,
        "instance_name": row.instance_name,
        "host": row.cluster_id,
        "project": row.project,
        "phase": row.phase,
        "status": row.status,
        "operation_id": row.operation_id,
        "error": row.error,
        "outcome": outcome,
        "result": row.result,
        "verified_readback": row.verified_spec,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _reject_sensitive_job_metadata(value: Any) -> None:
    """Keep the generic job foundation from becoming a second secret/config store."""
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).strip().lower() in _FORBIDDEN_JOB_METADATA_KEYS:
                raise ValueError(f"Sensitive or raw field '{key}' cannot be stored in an operation job")
            _reject_sensitive_job_metadata(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_sensitive_job_metadata(nested)


async def get_operation_job(job_id: str) -> Optional[InstanceOperationJob]:
    """Return one durable operation record without performing authorization."""
    return await _load_job(job_id)


async def get_operation_job_by_request(
    requester: str, idempotency_key: str
) -> Optional[InstanceOperationJob]:
    """Look up an idempotent replay before making another daemon call."""
    async with async_session_maker() as db:
        return (
            await db.execute(
                select(InstanceOperationJob).where(
                    InstanceOperationJob.requester == requester,
                    InstanceOperationJob.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()


async def create_operation_job(
    *,
    kind: str,
    host: str,
    project: str,
    instance_name: str,
    requester: str,
    idempotency_key: str,
    payload_hash: str,
    requested: Mapping[str, Any],
    target_organization_id: Optional[int] = None,
    target_organization_slug: Optional[str] = None,
    target_organization_resolution: str = "ambiguous",
    plan_revision: Optional[str] = None,
    apply_mode: Optional[str] = None,
    instance_type: Optional[str] = None,
    before_spec: Optional[Mapping[str, Any]] = None,
    target_spec: Optional[Mapping[str, Any]] = None,
) -> tuple[InstanceOperationJob, bool]:
    """Insert a pending job or return the prior response for the same idempotent request.

    Callers authorize ``host/project/instance`` before entering this helper. The database's two
    uniqueness constraints close races between workers: one request key has one meaning, and one
    target has at most one ambiguous or in-flight mutation.
    """
    if kind not in {"create", "specification"}:
        raise ValueError("Unsupported instance operation kind")
    if target_organization_resolution not in {"exact", "ambiguous", "unbound"}:
        raise ValueError("Unsupported target organization resolution")
    _reject_sensitive_job_metadata(requested)
    if before_spec is not None:
        _reject_sensitive_job_metadata(before_spec)
    if target_spec is not None:
        _reject_sensitive_job_metadata(target_spec)
    async with async_session_maker() as db:
        existing = (
            await db.execute(
                select(InstanceOperationJob).where(
                    InstanceOperationJob.requester == requester,
                    InstanceOperationJob.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.payload_hash != payload_hash:
                raise SpecificationConflict(
                    "This Idempotency-Key was already used for a different request."
                )
            return existing, False

        active = (
            await db.execute(
                select(InstanceOperationJob).where(
                    InstanceOperationJob.cluster_id == host,
                    InstanceOperationJob.project == project,
                    InstanceOperationJob.instance_name == instance_name,
                    InstanceOperationJob.status.in_(ACTIVE_STATUSES),
                )
            )
        ).scalar_one_or_none()
        if active is not None:
            raise SpecificationConflict(
                "Another operation on this instance is still running or awaiting reconciliation.",
                detail={"message": "Another instance operation is active.", "job_id": active.job_id},
            )

        row = InstanceOperationJob(
            job_id=secrets.token_hex(16),
            kind=kind,
            cluster_id=host,
            project=project,
            instance_name=instance_name,
            instance_type=instance_type,
            requester=requester,
            target_organization_id=target_organization_id,
            target_organization_slug=target_organization_slug,
            target_organization_resolution=target_organization_resolution,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            plan_revision=plan_revision,
            apply_mode=apply_mode,
            requested=dict(requested),
            before_spec=dict(before_spec) if before_spec is not None else None,
            target_spec=dict(target_spec) if target_spec is not None else None,
            phase="pending",
            status="pending",
        )
        db.add(row)
        try:
            # important: the outbox row references ``job_id`` rather than the database primary
            # key and has no ORM relationship to teach the unit-of-work its insert order. Flush
            # the parent first so PostgreSQL cannot schedule the outbox insert ahead of it. This
            # is still one transaction; a later failure rolls both rows back.
            await db.flush()
            enqueue_instance_operation_audit(db, job_id=row.job_id, stage="accepted")
            await db.commit()
        except IntegrityError:
            await db.rollback()
            winner = (
                await db.execute(
                    select(InstanceOperationJob).where(
                        InstanceOperationJob.requester == requester,
                        InstanceOperationJob.idempotency_key == idempotency_key,
                    )
                )
            ).scalar_one_or_none()
            if winner is not None and winner.payload_hash == payload_hash:
                return winner, False
            raise SpecificationConflict(
                "Another operation on this instance started at the same time."
            ) from None
        await db.refresh(row)
        return row, True


async def update_operation_job(job_id: str, **values: Any) -> Optional[InstanceOperationJob]:
    """Attach an operation id or terminal result to a generic create/specification job."""
    allowed = {
        "phase",
        "status",
        "operation_id",
        "result",
        "error",
        "verified_spec",
        "finished_at",
    }
    unexpected = set(values) - allowed
    if unexpected:
        raise ValueError(f"Unsupported operation job fields: {', '.join(sorted(unexpected))}")
    await _set_job(job_id, **values)
    return await _load_job(job_id)


async def _set_job(
    job_id: str,
    *,
    worker_id: Optional[str] = None,
    audit_stage: Optional[str] = None,
    **values: Any,
) -> bool:
    """Update a job, optionally fencing the write to its current lease owner."""
    values["updated_at"] = datetime.now(timezone.utc)
    predicates = (InstanceOperationJob.job_id == job_id,)
    if worker_id is not None:
        predicates = (*predicates, InstanceOperationJob.locked_by == worker_id)
    if audit_stage == "terminal":
        predicates = (*predicates, InstanceOperationJob.status.in_(ACTIVE_STATUSES))
    elif audit_stage is not None:
        raise ValueError(f"Unsupported operation job audit stage: {audit_stage}")
    async with async_session_maker() as db:
        operation_id = values.get("operation_id")
        if operation_id:
            row = (
                await db.execute(
                    select(InstanceOperationJob)
                    .where(*predicates)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            operation_ids = list(row.operation_ids or [])
            if operation_id not in operation_ids:
                operation_ids.append(operation_id)
            values["operation_ids"] = operation_ids
        result = await db.execute(
            update(InstanceOperationJob).where(*predicates).values(**values)
        )
        if result.rowcount and audit_stage is not None:
            enqueue_instance_operation_audit(db, job_id=job_id, stage=audit_stage)
        await db.commit()
        transitioned = bool(result.rowcount)
    # Queue bookkeeping must not evict a healthy inventory view. Invalidate only once a
    # daemon state transition is known or a terminal readback gives the UI a new truth.
    terminal_with_readback = (
        values.get("status") in {"success", "partial"}
        or (values.get("status") == "failure" and values.get("result") is not None)
    )
    # An ambiguous action (the daemon may or may not have applied it) leaves the cached
    # inventory unproven, the same as a readback would replace it.
    ambiguous_action = values.get("status") == "unknown" and values.get("result") is not None
    if transitioned and (
        values.get("stopped_by_job") is True
        or values.get("verified_spec") is not None
        or terminal_with_readback
        or ambiguous_action
    ):
        try:
            get_cache_manager().invalidate_instance_cache()
        except Exception as exc:  # noqa: BLE001 - cache health cannot change a durable verdict
            logger.warning(
                "Could not invalidate instance cache after specification job %s: %s",
                job_id,
                sanitize_error(str(exc)),
            )
    return transitioned


async def _claim_job(job_id: str, worker_id: str, *, include_unknown: bool = False) -> bool:
    now = datetime.now(timezone.utc)
    stale = now - timedelta(seconds=_LEASE_SECONDS)
    statuses = ("pending", "running", "unknown") if include_unknown else ("pending", "running")
    async with async_session_maker() as db:
        result = await db.execute(
            update(InstanceOperationJob)
            .where(
                InstanceOperationJob.job_id == job_id,
                InstanceOperationJob.status.in_(statuses),
                or_(
                    InstanceOperationJob.locked_at.is_(None),
                    InstanceOperationJob.locked_at < stale,
                ),
            )
            .values(locked_by=worker_id, locked_at=now, updated_at=now)
        )
        await db.commit()
        return bool(result.rowcount)


async def _lease_heartbeat(job_id: str, worker_id: str) -> None:
    """Keep a live worker's lease fresh while bounded daemon calls are outstanding."""
    while True:
        await asyncio.sleep(_LEASE_HEARTBEAT_SECONDS)
        try:
            renewed = await _set_job(
                job_id,
                worker_id=worker_id,
                locked_at=datetime.now(timezone.utc),
            )
        except Exception as exc:  # noqa: BLE001 - fencing still protects later writes
            logger.warning(
                "Could not renew specification job lease for %s: %s",
                job_id,
                sanitize_error(str(exc)),
            )
            continue
        if not renewed:
            return


async def _stop_lease_heartbeat(task: Optional[asyncio.Task[Any]]) -> None:
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _load_job(job_id: str) -> Optional[InstanceOperationJob]:
    async with async_session_maker() as db:
        return (
            await db.execute(select(InstanceOperationJob).where(InstanceOperationJob.job_id == job_id))
        ).scalar_one_or_none()


async def _terminal(
    row: InstanceOperationJob,
    status: str,
    *,
    phase: str,
    operation_id: Optional[str],
    result: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
) -> bool:
    now = datetime.now(timezone.utc)
    # ``operation_id`` is the operation for the current phase. It must never fall back to
    # ``row.operation_id`` because that may belong to a completed stop/apply phase. Historical
    # correlation remains available in ``operation_ids``.
    final_operation_id = operation_id
    transitioned = await _set_job(
        row.job_id,
        worker_id=getattr(row, "locked_by", None),
        audit_stage="terminal" if status in TERMINAL_STATUSES else None,
        status=status,
        phase=phase,
        result=result,
        error=error,
        operation_id=final_operation_id,
        locked_by=None,
        locked_at=None,
        finished_at=now if status in TERMINAL_STATUSES else None,
    )
    if not transitioned:
        return False
    if status == "unknown":
        # Unknown is deliberately not a terminal audit verdict. Reconciliation will emit the
        # single success/failure/partial record after actual daemon state can be established.
        return True
    try:
        await drain_instance_operation_audits(job_id=row.job_id)
    except Exception as exc:  # noqa: BLE001 - the outbox will be replayed
        logger.warning(
            "Could not drain terminal audit for specification job %s: %s",
            row.job_id,
            sanitize_error(str(exc)),
        )
    return True


async def _read_for_job(client: Any, row: InstanceOperationJob) -> SpecificationRead:
    return await _bounded(
        read_specification_sync,
        client,
        row.instance_name,
        row.cluster_id,
        row.project,
        timeout=_READ_TIMEOUT_SECONDS,
        label=f"read job target {row.cluster_id}/{row.project}/{row.instance_name}",
        concurrency_key=row.cluster_id,
    )


def _success_result(row: InstanceOperationJob, current: SpecificationRead) -> Dict[str, Any]:
    return {
        "specification": current.public,
        "guest_action_required": bool(
            current.public.get("type") == "virtual-machine"
            and (row.target_spec or {}).get("execution") == "live"
        ),
    }


async def _operation_verdict(
    client: Any, row: InstanceOperationJob
) -> Dict[str, Optional[str]]:
    try:
        return await _bounded(
            read_operation_verdict_sync,
            client,
            row.operation_id,
            row.project,
            timeout=_READ_TIMEOUT_SECONDS,
            label=f"read operation {row.cluster_id}/{row.operation_id or 'unknown'}",
            concurrency_key=row.cluster_id,
        )
    except Exception as exc:  # noqa: BLE001 - readback can still establish the truth
        return {"verdict": "unknown", "error": sanitize_error(str(exc))[:1000]}


async def _mark_unknown(
    row: InstanceOperationJob,
    *,
    action: str,
    operation_id: Optional[str],
    error: str,
    result_extra: Optional[Mapping[str, Any]] = None,
) -> None:
    result = {"ambiguous_action": action}
    if result_extra:
        result.update(result_extra)
    transitioned = await _terminal(
        row,
        "unknown",
        phase="unknown",
        result=result,
        error=error,
        operation_id=operation_id,
    )
    if transitioned:
        asyncio.create_task(_reconcile_unknown_later(row.job_id))


async def _read_before_automatic_start(
    row: InstanceOperationJob,
    client: Any,
    current: SpecificationRead,
    *,
    outcome_prefix: str,
) -> Optional[SpecificationRead]:
    """Return a fresh safe state or terminate without starting the instance.

    Incus applies ``volatile.<root>.apply_quota`` during start. An automatic start while that
    marker is present can turn an earlier logical size decrease into a physical filesystem
    shrink, so inability to prove the marker absent is a hard safety boundary.
    """
    try:
        fresh = await _read_for_job(client, row)
    except Exception as exc:  # noqa: BLE001 - an unreadable safety boundary stays stopped
        await _terminal(
            row,
            "partial",
            phase="partial",
            operation_id=row.operation_id,
            result={
                "specification": current.public,
                "instance_status": current.public.get("status") or "Unknown",
            },
            error=(
                f"{outcome_prefix}, but automatic start was withheld because the root-disk "
                f"safety state could not be read: {sanitize_error(str(exc))}"
            )[:1000],
        )
        return None

    if (fresh.public.get("root_disk") or {}).get("pending_quota_change"):
        await _terminal(
            row,
            "partial",
            phase="partial",
            operation_id=row.operation_id,
            result={
                "specification": fresh.public,
                "instance_status": fresh.public.get("status"),
            },
            error=(
                f"{outcome_prefix}, but automatic start was withheld because Incus reports "
                "a deferred root-disk quota change that could resize storage during start."
            )[:1000],
        )
        return None

    return fresh


async def _complete_restart_after_apply(
    row: InstanceOperationJob,
    client: Any,
    current: SpecificationRead,
    *,
    completion_status: str = "success",
    completion_error: Optional[str] = None,
) -> None:
    """Start a guest stopped by this job and verify that it is actually running."""
    if completion_status not in {"success", "partial"}:
        raise ValueError("Unsupported restart completion status")
    guarded = await _read_before_automatic_start(
        row,
        client,
        current,
        outcome_prefix="The specification was applied",
    )
    if guarded is None:
        return
    current = guarded
    current_status = current.public.get("status")
    if current_status == "Running":
        if _matches_target(current.public, row.requested or {}):
            await _terminal(
                row,
                completion_status,
                phase=completion_status,
                result=_success_result(row, current),
                error=completion_error,
                operation_id=row.operation_id,
            )
        else:
            await _terminal(
                row,
                "partial",
                phase="partial",
                result={
                    "specification": current.public,
                    "instance_status": current_status,
                },
                error=(
                    "The instance was already running before automatic restart, but verified "
                    "readback does not match every requested value."
                ),
                operation_id=row.operation_id,
            )
        return
    if current_status != "Stopped":
        await _terminal(
            row,
            "partial",
            phase="partial",
            result={"specification": current.public, "instance_status": current_status},
            error=(
                "The specification was applied, but automatic start was withheld because "
                f"the instance state is {current_status or 'Unknown'} rather than Stopped."
            ),
            operation_id=row.operation_id,
        )
        return
    if not await _set_job(
        row.job_id,
        worker_id=getattr(row, "locked_by", None),
        status="running",
        phase="starting",
        operation_id=None,
        result={
            "ambiguous_action": "start",
            "completion_status": completion_status,
            "completion_error": completion_error,
        },
    ):
        return
    operation_id = None
    try:
        start_op = await _bounded(
            change_instance_state_sync,
            client,
            row.instance_name,
            row.project,
            "start",
            timeout=_STATE_TIMEOUT_SECONDS,
            label=f"start instance {row.cluster_id}/{row.project}/{row.instance_name}",
            concurrency_key=row.cluster_id,
        )
        operation_id = start_op
        if not await _set_job(
            row.job_id,
            worker_id=getattr(row, "locked_by", None),
            phase="starting",
            operation_id=operation_id,
        ):
            return
    except (SpecificationWriteUnknown, SpecificationTimeout) as exc:
        await _mark_unknown(
            row,
            action="start",
            operation_id=getattr(exc, "operation_id", None),
            error=(
                "The specification was applied, but the final start state is unknown. "
                "The backend will reconcile the operation and instance state."
            ),
            result_extra={
                "completion_status": completion_status,
                "completion_error": completion_error,
            },
        )
        return
    except Exception as exc:  # noqa: BLE001 - applied but not restarted is partial
        await _terminal(
            row,
            "partial",
            phase="partial",
            result={"specification": current.public, "instance_status": "Stopped"},
            error=(
                "The specification was applied, but the instance could not be restarted: "
                f"{sanitize_error(str(exc))}"
            )[:1000],
            operation_id=operation_id,
        )
        return

    try:
        verified = await _read_for_job(client, row)
    except Exception as exc:  # noqa: BLE001 - terminal start without readback stays ambiguous
        await _mark_unknown(
            row,
            action="start",
            operation_id=operation_id,
            error=(
                "The start operation completed, but its final instance state could not be "
                f"verified: {sanitize_error(str(exc))}"
            )[:1000],
            result_extra={
                "completion_status": completion_status,
                "completion_error": completion_error,
            },
        )
        return
    if verified.public.get("status") != "Running":
        await _terminal(
            row,
            "partial",
            phase="partial",
            result={
                "specification": verified.public,
                "instance_status": verified.public.get("status"),
            },
            error="The specification was applied, but the instance did not return to Running.",
            operation_id=operation_id,
        )
        return
    if (
        not _matches_target(verified.public, row.requested or {})
        or (verified.public.get("root_disk") or {}).get("pending_quota_change")
    ):
        await _terminal(
            row,
            "partial",
            phase="partial",
            result={
                "specification": verified.public,
                "instance_status": "Running",
            },
            error=(
                "The instance returned to Running, but verified readback does not match every "
                "requested value or reports a deferred root-disk quota change."
            ),
            operation_id=operation_id,
        )
        return
    await _terminal(
        row,
        completion_status,
        phase=completion_status,
        result=_success_result(row, verified),
        error=completion_error,
        operation_id=operation_id,
    )


async def _restore_after_failed_apply(
    row: InstanceOperationJob,
    client: Any,
    current: SpecificationRead,
    apply_error: str,
) -> None:
    """Restore a guest stopped by the job after a known update failure."""
    guarded = await _read_before_automatic_start(
        row,
        client,
        current,
        outcome_prefix=f"The specification update failed ({apply_error})",
    )
    if guarded is None:
        return
    current = guarded
    current_status = current.public.get("status")
    if current_status == "Running":
        await _terminal(
            row,
            "failure",
            phase="failure",
            result={"specification": current.public, "instance_status": "Running"},
            error=apply_error,
            operation_id=row.operation_id,
        )
        return
    if current_status != "Stopped":
        await _terminal(
            row,
            "partial",
            phase="partial",
            result={"specification": current.public, "instance_status": current_status},
            error=(
                f"The specification update failed ({apply_error}), and automatic restore "
                f"was withheld because the instance state is {current_status or 'Unknown'} "
                "rather than Stopped."
            )[:1000],
            operation_id=row.operation_id,
        )
        return
    if not await _set_job(
        row.job_id,
        worker_id=getattr(row, "locked_by", None),
        status="running",
        phase="starting",
        operation_id=None,
        result={
            "ambiguous_action": "restore_start",
            "apply_error": apply_error,
        },
    ):
        return
    operation_id = None
    try:
        start_op = await _bounded(
            change_instance_state_sync,
            client,
            row.instance_name,
            row.project,
            "start",
            timeout=_STATE_TIMEOUT_SECONDS,
            label=f"restore instance {row.cluster_id}/{row.project}/{row.instance_name}",
            concurrency_key=row.cluster_id,
        )
        operation_id = start_op
        if not await _set_job(
            row.job_id,
            worker_id=getattr(row, "locked_by", None),
            phase="starting",
            operation_id=operation_id,
        ):
            return
    except (SpecificationWriteUnknown, SpecificationTimeout) as exc:
        await _mark_unknown(
            row,
            action="restore_start",
            operation_id=getattr(exc, "operation_id", None),
            error=(
                f"The specification update failed ({apply_error}), and the final restore "
                "state is unknown. The backend will reconcile actual state."
            )[:1000],
        )
        return
    except Exception as exc:  # noqa: BLE001
        await _terminal(
            row,
            "partial",
            phase="partial",
            result={"specification": current.public, "instance_status": "Stopped"},
            error=(
                f"The specification update failed ({apply_error}) and the instance could not "
                f"be restarted: {sanitize_error(str(exc))}"
            )[:1000],
            operation_id=operation_id,
        )
        return

    try:
        restored = await _read_for_job(client, row)
    except Exception as exc:  # noqa: BLE001
        await _mark_unknown(
            row,
            action="restore_start",
            operation_id=operation_id,
            error=(
                f"The specification update failed ({apply_error}); the restore operation "
                f"completed but readback failed: {sanitize_error(str(exc))}"
            )[:1000],
        )
        return
    if restored.public.get("status") != "Running":
        await _terminal(
            row,
            "partial",
            phase="partial",
            result={
                "specification": restored.public,
                "instance_status": restored.public.get("status"),
            },
            error=(
                f"The specification update failed ({apply_error}) and the instance did not "
                "return to Running."
            )[:1000],
            operation_id=operation_id,
        )
        return
    await _terminal(
        row,
        "failure",
        phase="failure",
        result={"specification": restored.public, "instance_status": "Running"},
        error=apply_error,
        operation_id=operation_id,
    )


async def _reconcile_unknown(row: InstanceOperationJob, client: Any) -> None:
    """Correlate the daemon operation and actual state without replaying ambiguity."""
    operation = await _operation_verdict(client, row)
    try:
        current = await _read_for_job(client, row)
    except Exception as exc:  # noqa: BLE001 - ambiguity remains explicit
        released = await _set_job(
            row.job_id,
            worker_id=getattr(row, "locked_by", None),
            locked_by=None,
            locked_at=None,
            error=sanitize_error(str(exc))[:1000],
        )
        if released:
            asyncio.create_task(_reconcile_unknown_later(row.job_id, delay=15))
        return

    stored_result = row.result if isinstance(row.result, dict) else {}
    action = str(
        stored_result.get("ambiguous_action")
        or {
            "stopping": "stop",
            "submitting": "apply",
            "verifying": "apply",
            "starting": "starting",
        }.get(row.phase, "apply")
    )
    target_matches = _matches_target(current.public, row.requested or {})
    instance_status = current.public.get("status")
    if action == "starting":
        action = "start" if target_matches else "restore_start"

    verdict = operation["verdict"]
    # A known operation must reach a terminal verdict before readback can authorize a restart
    # or a success. With no operation id, an ``unknown`` verdict is not evidence that a timed-out
    # daemon write stopped running, so the target remains exclusive for later reconciliation.
    pending_verdict = verdict == "running" or (
        verdict == "unknown" and bool(row.operation_id)
    )
    missing_verdict = verdict == "unknown" and not row.operation_id
    fallback_verdict = verdict == "expired"

    async def release_for_reconciliation() -> None:
        released = await _set_job(
            row.job_id,
            worker_id=getattr(row, "locked_by", None),
            locked_by=None,
            locked_at=None,
        )
        if released:
            asyncio.create_task(_reconcile_unknown_later(row.job_id, delay=15))

    if action == "stop":
        if pending_verdict or (missing_verdict and instance_status != "Stopped"):
            await release_for_reconciliation()
            return
        if verdict == "success" and instance_status == "Stopped":
            released = await _set_job(
                row.job_id,
                worker_id=getattr(row, "locked_by", None),
                status="running",
                phase="applying",
                stopped_by_job=True,
                result=None,
                locked_by=None,
                locked_at=None,
            )
            if released:
                asyncio.create_task(run_specification_job(row.job_id))
            return
        stop_error = operation["error"] or (
            "The graceful-stop operation expired before its terminal verdict was recorded."
            if fallback_verdict
            else "The graceful stop could not be correlated to a daemon operation."
        )
        if instance_status == "Stopped":
            await _restore_after_failed_apply(
                row,
                client,
                current,
                stop_error,
            )
            return
        if verdict in {"success", "failure", "expired"}:
            await _terminal(
                row,
                "failure",
                phase="failure",
                operation_id=row.operation_id,
                result={"specification": current.public, "instance_status": instance_status},
                error=(
                    operation["error"]
                    or "The graceful stop did not leave the instance stopped. No update was submitted."
                ),
            )
            return
    elif action in {"start", "restore_start"}:
        if pending_verdict or missing_verdict:
            await release_for_reconciliation()
            return
        if instance_status == "Running":
            if target_matches:
                requested_completion = str(
                    stored_result.get("completion_status") or "success"
                )
                success = verdict == "success" and requested_completion == "success"
                status = "success" if success else "partial"
                await _terminal(
                    row,
                    status,
                    phase=status,
                    operation_id=row.operation_id,
                    result=_success_result(row, current),
                    error=(
                        None
                        if success
                        else stored_result.get("completion_error")
                        or operation["error"]
                        or "The instance is running with the requested specification, but the start operation's terminal success could not be proven."
                    ),
                )
            else:
                await _terminal(
                    row,
                    "failure",
                    phase="failure",
                    operation_id=row.operation_id,
                    error="The specification was not applied; the original running state was restored.",
                )
            return
        if verdict in {"success", "failure", "expired"}:
            await _terminal(
                row,
                "partial",
                phase="partial",
                operation_id=row.operation_id,
                result={"specification": current.public, "instance_status": instance_status},
                error=(
                    "The instance could not be restarted after the specification operation: "
                    f"{operation['error'] or 'the daemon refused the start'}"
                )[:1000],
            )
            return
    else:
        if pending_verdict or missing_verdict:
            await release_for_reconciliation()
            return
        if target_matches:
            completion_status = "success" if verdict == "success" else "partial"
            completion_error = None if completion_status == "success" else (
                operation["error"]
                or "The requested specification is visible, but terminal daemon success could not be proven."
            )
            if row.stopped_by_job and instance_status == "Stopped":
                await _complete_restart_after_apply(
                    row,
                    client,
                    current,
                    completion_status=completion_status,
                    completion_error=completion_error,
                )
            else:
                await _terminal(
                    row,
                    completion_status,
                    phase=completion_status,
                    operation_id=row.operation_id,
                    result=_success_result(row, current),
                    error=completion_error,
                )
            return
        if verdict == "success":
            await _terminal(
                row,
                "partial",
                phase="partial",
                operation_id=row.operation_id,
                result={"specification": current.public, "instance_status": instance_status},
                error="The daemon reported success, but verified readback does not match every requested value.",
            )
            return
        if verdict in {"failure", "expired"}:
            apply_error = operation["error"] or (
                "The specification operation expired before its terminal verdict was recorded."
                if fallback_verdict
                else "The daemon refused the specification update."
            )
            if row.stopped_by_job and instance_status == "Stopped":
                await _restore_after_failed_apply(
                    row,
                    client,
                    current,
                    apply_error,
                )
                return
            await _terminal(
                row,
                "failure",
                phase="failure",
                operation_id=row.operation_id,
                result={"specification": current.public, "instance_status": instance_status},
                error=apply_error,
            )
            return

    await release_for_reconciliation()


async def _reconcile_unknown_later(job_id: str, *, delay: int = 5) -> None:
    # Give the daemon operation a short window to settle. If this process disappears first,
    # startup recovery performs the same read-only reconciliation from the durable row.
    await asyncio.sleep(delay)
    await run_specification_job(job_id, recover_unknown=True)


async def _resume_job_later(job_id: str, *, delay: int = 15) -> None:
    """Retry a known active phase after a transient control-plane outage."""
    await asyncio.sleep(delay)
    await run_specification_job(job_id)


async def _run_claimed_specification_job(
    row: InstanceOperationJob,
    worker_id: str,
) -> None:
    """Drive one claimed job. Every state write is fenced to ``worker_id``."""
    try:
        client = await get_client(row.cluster_id)
    except Exception as exc:  # noqa: BLE001 - durable failure record
        ambiguous_phase = row.phase in {
            "stopping",
            "applying",
            "submitting",
            "starting",
            "verifying",
            "unknown",
        }
        if row.status == "unknown" or row.stopped_by_job or ambiguous_phase:
            # A cluster outage cannot turn a previously ambiguous mutation into a known
            # failure. Retain target exclusivity and retry read-only reconciliation later.
            released = await _set_job(
                row.job_id,
                worker_id=worker_id,
                locked_by=None,
                locked_at=None,
                error=sanitize_error(str(exc))[:1000],
            )
            if released:
                retry = (
                    _reconcile_unknown_later
                    if row.status == "unknown"
                    else _resume_job_later
                )
                asyncio.create_task(retry(row.job_id, delay=15))
            return
        await _terminal(
            row,
            "failure",
            phase="failure",
            operation_id=None,
            error=sanitize_error(str(exc))[:1000],
        )
        return

    lock = _cluster_locks.setdefault(row.cluster_id, asyncio.Lock())
    async with lock:
        ambiguity_phases = {"stopping", "submitting", "starting", "verifying"}
        if row.status == "unknown" or row.phase in ambiguity_phases:
            await _reconcile_unknown(row, client)
            return

        stopped_by_job = bool(row.stopped_by_job)
        operation_id = row.operation_id
        current: Optional[SpecificationRead] = None
        try:
            state_update: Dict[str, Any] = {"status": "running"}
            # Only a never-submitted job may advertise preflight before its read completes.
            # Recovery phases are durable crash markers and must survive another process death.
            if row.phase == "pending":
                state_update["phase"] = "preflighting"
            if not await _set_job(
                row.job_id,
                worker_id=worker_id,
                **state_update,
            ):
                return
            current = await _read_for_job(client, row)
            current_status = current.public.get("status")
            target_matches = _matches_target(current.public, row.requested or {})

            if target_matches:
                # No daemon specification operation belongs to a pending/preflight/applying
                # phase. A matching state may have been changed externally; report that truth
                # without claiming this job observed a successful terminal operation.
                completion_error = (
                    "The requested specification is present, but this job did not record a "
                    "terminal daemon update operation."
                )
                if (
                    (row.original_status == "Running" or stopped_by_job)
                    and current_status == "Stopped"
                ):
                    stopped_by_job = True
                    if not await _set_job(
                        row.job_id,
                        worker_id=worker_id,
                        stopped_by_job=True,
                    ):
                        return
                    await _complete_restart_after_apply(
                        row,
                        client,
                        current,
                        completion_status="partial",
                        completion_error=completion_error,
                    )
                else:
                    await _terminal(
                        row,
                        "partial",
                        phase="partial",
                        operation_id=None,
                        result=_success_result(row, current),
                        error=completion_error,
                    )
                return

            controlled_stopped = bool(
                current_status == "Stopped"
                and row.original_status == "Running"
                and (stopped_by_job or row.phase in {"stopping", "applying"})
            )
            if controlled_stopped:
                stopped_by_job = True
                if not await _set_job(
                    row.job_id,
                    worker_id=worker_id,
                    stopped_by_job=True,
                    phase="applying",
                    result=None,
                ):
                    return

            if (
                stopped_by_job
                and (current.public.get("root_disk") or {}).get(
                    "pending_quota_change"
                )
            ):
                await _terminal(
                    row,
                    "partial",
                    phase="partial",
                    operation_id=operation_id,
                    result={
                        "specification": current.public,
                        "instance_status": current_status,
                    },
                    error=(
                        "The job stopped the instance, but Incus now reports a deferred "
                        "root-disk quota change. No specification update was submitted and "
                        "automatic start was withheld."
                    ),
                )
                return

            usage = await _bounded(
                read_project_usage,
                client,
                row.project,
                timeout=_READ_TIMEOUT_SECONDS,
                label=f"recheck project usage {row.cluster_id}/{row.project}",
                concurrency_key=row.cluster_id,
            )
            initial_plan = build_preflight(current, row.requested or {}, project_usage=usage)
            if (
                initial_plan["plan_revision"] != row.plan_revision
                and not controlled_stopped
            ):
                raise SpecificationConflict(
                    "The specification plan became stale before it could be applied."
                )
            if initial_plan["blockers"]:
                raise SpecificationConflict(
                    "The specification change is blocked.", detail=initial_plan
                )

            if controlled_stopped:
                pass
            elif initial_plan["execution"] == "restart_required":
                if row.apply_mode != "graceful_restart":
                    raise SpecificationConflict(
                        "This running instance requires explicit graceful-restart approval."
                    )
                if not await _set_job(
                    row.job_id,
                    worker_id=worker_id,
                    phase="stopping",
                    original_status="Running",
                    operation_id=None,
                    result={"ambiguous_action": "stop"},
                ):
                    return
                operation_id = None
                try:
                    operation_id = await _bounded(
                        change_instance_state_sync,
                        client,
                        row.instance_name,
                        row.project,
                        "stop",
                        timeout=_STATE_TIMEOUT_SECONDS,
                        label=(
                            f"stop instance {row.cluster_id}/{row.project}/{row.instance_name}"
                        ),
                        concurrency_key=row.cluster_id,
                    )
                except (SpecificationWriteUnknown, SpecificationTimeout) as exc:
                    await _mark_unknown(
                        row,
                        action="stop",
                        operation_id=getattr(exc, "operation_id", None),
                        error=(
                            "The final graceful-stop state is unknown. The backend will "
                            "reconcile actual state before applying any specification change."
                        ),
                    )
                    return
                stopped_by_job = True
                if not await _set_job(
                    row.job_id,
                    worker_id=worker_id,
                    phase="applying",
                    stopped_by_job=True,
                    operation_id=operation_id,
                    result=None,
                ):
                    return
                current = await _read_for_job(client, row)
                if current.public.get("status") != "Stopped":
                    await _mark_unknown(
                        row,
                        action="stop",
                        operation_id=operation_id,
                        error=(
                            "The graceful-stop operation finished, but the instance was not "
                            "observed as Stopped. No specification write was submitted."
                        ),
                    )
                    return
            if not await _set_job(
                row.job_id,
                worker_id=worker_id,
                phase="submitting",
                operation_id=None,
                result={"ambiguous_action": "apply"},
            ):
                return
            operation_id = None
            try:
                verified, operation_id = await _bounded(
                    apply_specification_sync,
                    client,
                    instance_name=row.instance_name,
                    host=row.cluster_id,
                    project=row.project,
                    expected_etag=current.etag or "",
                    # The persisted revision was checked above, while the instance still had
                    # its original runtime state. A deliberate graceful stop changes execution
                    # from restart_required to stopped, so recomputing that revision here would
                    # reject the exact transition this worker just performed.
                    expected_plan_revision="",
                    requested=row.requested or {},
                    acknowledgements=(row.target_spec or {}).get("acknowledgements", []),
                    timeout=_WRITE_TIMEOUT_SECONDS,
                    label=f"apply specification {row.cluster_id}/{row.project}/{row.instance_name}",
                    concurrency_key=row.cluster_id,
                )
            except (SpecificationWriteUnknown, SpecificationTimeout) as exc:
                await _mark_unknown(
                    row,
                    action="apply",
                    error=(
                        "The cluster accepted the request but its final state is unknown. "
                        "The backend will reconcile actual state before any retry."
                    ),
                    operation_id=getattr(exc, "operation_id", None),
                )
                return

            if not await _set_job(
                row.job_id,
                worker_id=worker_id,
                phase="verifying",
                operation_id=operation_id,
                result={"ambiguous_action": "apply"},
                verified_spec=verified.public,
            ):
                return
            if stopped_by_job:
                await _complete_restart_after_apply(row, client, verified)
                return

            await _terminal(
                row,
                "success",
                phase="success",
                result=_success_result(row, verified),
                operation_id=operation_id,
            )
        except Exception as exc:  # noqa: BLE001 - every failure must become a durable verdict
            error = sanitize_error(str(exc))[:1000]
            if current is None and (stopped_by_job or row.phase == "applying"):
                released = await _set_job(
                    row.job_id,
                    worker_id=worker_id,
                    locked_by=None,
                    locked_at=None,
                    error=error,
                )
                if released:
                    asyncio.create_task(_resume_job_later(row.job_id, delay=15))
                return
            if stopped_by_job and current is not None:
                await _restore_after_failed_apply(row, client, current, error)
                return
            await _terminal(
                row,
                "failure",
                phase="failure",
                error=error,
                operation_id=operation_id,
            )


async def run_specification_job(job_id: str, *, recover_unknown: bool = False) -> None:
    """Claim and drive one job. Recovery observes daemon state before any mutation."""
    worker_id = f"{socket.gethostname()}:{secrets.token_hex(8)}"
    if not await _claim_job(job_id, worker_id, include_unknown=recover_unknown):
        return
    row = await _load_job(job_id)
    if row is None:
        return
    # The atomic claim above is authoritative. Keep the detached row's fence token aligned so
    # helper functions can guard every later state transition with the same lease owner.
    row.locked_by = worker_id
    heartbeat = asyncio.create_task(_lease_heartbeat(job_id, worker_id))
    try:
        # The guard stays held through every possible stop/apply/start submission and its
        # authoritative readback. Migration final preflight uses the same advisory lock.
        async with guard_instance_mutation(
            cluster_id=row.cluster_id,
            project=row.project,
            instance_name=row.instance_name,
        ):
            await _run_claimed_specification_job(row, worker_id)
    except MigrationFenceConflict as exc:
        prior_mutation = bool(
            row.stopped_by_job
            or row.operation_id
            or row.phase not in {"pending", "preflighting"}
        )
        await _terminal(
            row,
            "partial" if prior_mutation else "failure",
            phase="partial" if prior_mutation else "failure",
            operation_id=row.operation_id,
            result={"migration_fenced": True, "prior_phase": row.phase},
            error=exc.detail,
        )
    finally:
        await _stop_lease_heartbeat(heartbeat)


async def recover_instance_operation_jobs() -> int:
    """Schedule unleased/stale jobs; an atomic claim prevents duplicate workers."""
    stale = datetime.now(timezone.utc) - timedelta(seconds=_LEASE_SECONDS)
    async with async_session_maker() as db:
        rows = (
            await db.execute(
                select(InstanceOperationJob.job_id, InstanceOperationJob.status).where(
                    InstanceOperationJob.kind == "specification",
                    InstanceOperationJob.status.in_(ACTIVE_STATUSES),
                    or_(
                        InstanceOperationJob.locked_at.is_(None),
                        InstanceOperationJob.locked_at < stale,
                    ),
                )
            )
        ).all()
    for job_id, status in rows:
        asyncio.create_task(run_specification_job(job_id, recover_unknown=status == "unknown"))
    return len(rows)
