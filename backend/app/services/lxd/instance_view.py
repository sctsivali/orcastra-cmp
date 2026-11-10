"""How an instance is presented to a key-authenticated caller.

Kept apart from the operation dispatcher because it is presentation only. The REST list, the
REST detail read and the proxy's ``get_instance`` share this one serialiser, so all three agree
on field names.
"""

from typing import Any, Dict, List, Optional

__all__ = [
    "LXD_STATUS_NAMES",
    "instance_detail",
    "instance_summary",
    "status_name",
]


# LXD's own status codes, which are lossless where the dashboard's VMStatus enum is not.
# `parse_vm_status` (services/lxd/utils.py) maps four names and collapses the rest to UNKNOWN,
# so a caller waiting for an instance to leave "Starting" could not tell it from a fault.
LXD_STATUS_NAMES: Dict[int, str] = {
    100: "Operation created", 101: "Started", 102: "Stopped", 103: "Running",
    104: "Cancelling", 105: "Pending", 106: "Starting", 107: "Stopping",
    108: "Aborting", 109: "Freezing", 110: "Frozen", 111: "Thawed", 112: "Error",
    200: "Success", 400: "Failure", 401: "Cancelled",
}


def status_name(status_code: Optional[int], fallback: Any) -> Any:
    """The status LXD reported, preferring its code over a lossy enum round trip."""
    if status_code in LXD_STATUS_NAMES:
        return LXD_STATUS_NAMES[status_code]
    return getattr(fallback, "value", fallback)


# ── serialising an instance ────────────────────────────────────────────────────────────────


def _resources_from(
    config: Optional[Dict[str, Any]], expanded_devices: Optional[Dict[str, Any]]
) -> Dict[str, Optional[str]]:
    """CPU, memory and root disk as LXD records them.

    The effective root disk lives in ``expanded_devices``: an instance that overrides nothing
    inherits its root disk from a profile, so ``devices`` alone reports None.
    """
    config = config or {}
    root = (expanded_devices or {}).get("root") or {}
    return {
        "cpu": config.get("limits.cpu"),
        "memory": config.get("limits.memory"),
        "disk": root.get("size") or root.get("size.state"),
    }


def instance_summary(
    *,
    name: str,
    cluster_id: str,
    project: Optional[str],
    status: Optional[str],
    instance_type: Optional[str],
    architecture: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
    expanded_devices: Optional[Dict[str, Any]] = None,
    status_code: Optional[int] = None,
    ip_addresses: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Enough for a caller to pick a target without a second round trip."""
    return {
        "name": name,
        "cluster_id": cluster_id,
        "project": project or "default",
        "status": status,
        "status_code": status_code,
        "type": instance_type,
        "architecture": architecture,
        "ip_addresses": list(ip_addresses or []),
        **_resources_from(config, expanded_devices),
    }


def instance_detail(
    *,
    devices: Optional[Dict[str, Any]] = None,
    expanded_devices: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    profiles: Optional[List[str]] = None,
    location: Optional[str] = None,
    description: Optional[str] = None,
    created_at: Any = None,
    last_used_at: Any = None,
    **summary_fields: Any,
) -> Dict[str, Any]:
    """The full specification, for a caller that has already chosen its target."""
    detail = instance_summary(
        config=config, expanded_devices=expanded_devices, **summary_fields
    )
    detail.update(
        {
            "config": dict(config or {}),
            "devices": dict(devices or {}),
            "expanded_devices": dict(expanded_devices or {}),
            "profiles": list(profiles or []),
            "location": location,
            "description": description,
            "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
            "last_used_at": (
                last_used_at.isoformat() if hasattr(last_used_at, "isoformat") else last_used_at
            ),
        }
    )
    return detail
