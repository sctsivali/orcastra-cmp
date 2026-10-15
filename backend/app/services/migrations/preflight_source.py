"""Source-side instance checks for migration preflight: state, devices, in-flight operations.

Split out of ``preflight_instance``. Readers and inventory errors still come from ``preflight``,
looked up at call time so tests can replace them.
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

_HARDWARE_DEVICES = {"gpu", "pci", "usb", "unix-block", "unix-char", "unix-hotplug", "infiniband"}
_ALLOWED_DEVICES = {"disk", "nic", "none", "tpm"}
_INSTANCE_ROOTS = {"instances", "containers", "virtual-machines"}


def _pf() -> Any:
    from . import preflight

    return preflight


def source_status(instance: dict[str, Any]) -> str:
    return str(instance.get("status") or "").strip().lower()


def source_state(result: Any, instance: dict[str, Any], name: str, settings: Any) -> str:
    status = source_status(instance)
    running_allowed = bool(getattr(settings, "migration_running_source_enabled", True))
    if status == "stopped" or (status == "running" and running_allowed):
        return status
    if status == "running":
        result.block(
            "source_instance_not_stopped",
            f"Instance '{name}' must be stopped before migration on this deployment.",
            "Stop the instance, or enable MIGRATION_RUNNING_SOURCE_ENABLED so Orcastra stops "
            "and restarts it around the transfer.",
        )
    else:
        result.block(
            "source_instance_state_unsupported",
            f"Instance '{name}' is {status or 'in an unknown state'}. Only running or stopped "
            "instances can be migrated.",
            "Wait for the instance to settle in Running or Stopped, then run preflight again.",
        )
    return status


def device_findings(result: Any, instance: dict[str, Any], name: str, intent: str) -> None:
    # Expanded values include inherited profile configuration, so a privileged or raw profile
    # cannot cross the boundary without appearing here.
    config = instance.get("expanded_config") or instance.get("config") or {}
    if any(str(key).startswith("raw.") for key in config):
        result.block(
            "raw_instance_config",
            f"Instance '{name}' has raw daemon configuration tied to its current host.",
            "Remove the raw.* keys or move the instance with a separately reviewed procedure.",
        )
    if str(config.get("security.privileged", "")).lower() == "true":
        result.warn(
            "privileged_instance",
            f"Instance '{name}' is privileged. The target project must allow privileged "
            "containers.",
        )
    if intent == "move" and str(config.get("boot.autostart", "")).lower() == "true":
        result.warn(
            "source_autostart_fenced",
            f"Instance '{name}' starts at boot. The retained source has boot.autostart turned "
            "off during retention, and rollback restores it.",
        )
    devices = instance.get("expanded_devices") or instance.get("devices") or {}
    for device_name, device in devices.items():
        if not isinstance(device, dict):
            result.block("invalid_device", f"Instance '{name}' has invalid device '{device_name}'.")
            continue
        device_type = str(device.get("type") or "")
        if device_type in _HARDWARE_DEVICES:
            result.block(
                "unsupported_device",
                f"Instance '{name}' uses host hardware device '{device_name}' ({device_type}).",
                "Remove the device before migration and attach equivalent hardware on the target.",
            )
        elif device_type == "proxy":
            result.block(
                "unsupported_device",
                f"Instance '{name}' has proxy device '{device_name}', which binds host ports.",
                "Remove the proxy device and recreate it on the target after migration.",
            )
        elif device_type not in _ALLOWED_DEVICES:
            result.block(
                "unknown_or_unsupported_device",
                f"Instance '{name}' uses unapproved device type '{device_type or 'unknown'}'.",
                "Remove it or migrate the instance with a separately reviewed procedure.",
            )
        if device_type == "disk" and device.get("path") != "/" and device.get("source"):
            result.block(
                "attached_volume_or_host_path",
                f"Instance '{name}' has attached disk '{device_name}' (custom volume or host path).",
                "Detach it before migration, or migrate the volume first and reattach it after.",
            )


def _operation_instances(client: Any, raw_url: Any, default_project: str) -> Optional[tuple[str, str]]:
    if not isinstance(raw_url, str) or not raw_url or len(raw_url) > 4096:
        raise _pf().InventoryReadError("active operation ownership")
    parsed = urlparse(raw_url)
    segments = [unquote(segment) for segment in parsed.path.split("/") if segment]
    if len(segments) < 3 or segments[0] != "1.0" or segments[1] not in _INSTANCE_ROOTS:
        return None
    query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=32)
    project_values = query.get("project") or [default_project]
    return project_values[0].strip() or "default", segments[2]


def active_instance_operations(client: Any, wanted: set[tuple[str, str]]) -> set[tuple[str, str]]:
    """Return which of the given (project, instance) pairs a non-terminal operation touches.

    Daemon housekeeping tasks without instance resources are ignored on purpose: they do not
    write the instances being migrated, and treating them as activity blocked every migration
    on a host with a stuck background task.
    """
    pf = _pf()
    extensions = pf._authoritative_extensions(client)
    try:
        node = client.api.operations
    except (AttributeError, TypeError) as exc:
        raise pf.InventoryReadError("active operation") from exc
    projects = sorted({project for project, _name in wanted})
    try:
        if "operations_get_query_all_projects" in extensions:
            responses = [node.get(params={"recursion": 1, "all-projects": "true"})]
        else:
            responses = [node.get(params={"project": project, "recursion": 1}) for project in projects]
    except Exception as exc:  # noqa: BLE001
        raise pf.InventoryReadError("active operation") from exc
    touched: set[tuple[str, str]] = set()
    for response in responses:
        metadata = pf._metadata(response, resource="active operation")
        if not isinstance(metadata, dict):
            raise pf.InventoryReadError("active operation")
        for bucket, operations in metadata.items():
            if str(bucket).strip().lower() in pf._TERMINAL_OPERATION_BUCKETS:
                continue
            if not isinstance(operations, list):
                raise pf.InventoryReadError("active operation")
            for operation in operations:
                if not isinstance(operation, dict):
                    raise pf.InventoryReadError("active operation")
                default_project = str(operation.get("project") or "default")
                for urls in (operation.get("resources") or {}).values():
                    for raw_url in urls or []:
                        owner = _operation_instances(client, raw_url, default_project)
                        if owner in wanted:
                            touched.add(owner)
    return touched


def check_instance_operations(
    result: Any,
    *,
    source_client: Any,
    target_client: Any,
    same_cluster: bool,
    source_project: str,
    source_name: str,
    target_project: str,
    target_name: str,
) -> None:
    pf = _pf()
    sides = [("source", source_client, {(source_project, source_name)})]
    if same_cluster:
        sides[0][2].add((target_project, target_name))
    else:
        sides.append(("target", target_client, {(target_project, target_name)}))
    for side, client, wanted in sides:
        try:
            touched = active_instance_operations(client, wanted)
        except pf.InventoryReadError as exc:
            result.capabilities["daemon_operation_inventory_verified"] = False
            result.capabilities["daemon_operations_quiescent"] = False
            pf._block_inventory_error(result, side=side, error=exc)
            continue
        for project, name in sorted(touched):
            result.capabilities["daemon_operations_quiescent"] = False
            result.block(
                f"{side}_active_daemon_operation",
                f"An operation is still running on instance '{name}' in project '{project}'.",
                "Wait for it to finish and run preflight again. Active operations are never "
                "assumed safe.",
            )


__all__ = ["active_instance_operations", "check_instance_operations", "device_findings", "source_state", "source_status"]
