"""Journal rows on the wire, with the field policy of the caller's tier applied.

Every declared field of `TaskRecord` and `ClusterLogEntry` is set here explicitly, so a column
the models gain and this module does not know about is a test failure rather than a default
the frontend types and builds a control on. The tier is an argument: this package never reads
the request scope itself.

Field policy (D12): `target_organization_id/slug` and `requestor.address` reach Admin only;
`location` reaches Admin and Partner; a tenant's `metadata` and `details` are cut to the
allowlist below. Nothing here can reveal what was never stored (`fds`, exec command lines,
audit before/after bodies).
"""

from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

TENANT_DETAIL_KEYS = frozenset(
    {"operation_id", "image", "instance_type", "profiles", "project", "automated", "schedule_id", "snapshot"}
)


def _mapping(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _optional_str(value: Any) -> Optional[str]:
    return None if value is None else str(value)


def _tenant_cut(details: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in details.items() if key in TENANT_DETAIL_KEYS}


def _requestor(value: Any, tier: str) -> Optional[Dict[str, Optional[str]]]:
    source = _mapping(value)
    if not source:
        return None
    return {
        "username": _optional_str(source.get("username")),
        "protocol": _optional_str(source.get("protocol")),
        "address": _optional_str(source.get("address")) if tier == "admin" else None,
    }


def duration_ms(
    started_at: Optional[datetime],
    finished_at: Optional[datetime],
    now: Optional[datetime] = None,
) -> Optional[int]:
    """finished - started once finished, now - started while open, None when never started."""
    if started_at is None:
        return None
    end = finished_at or now or datetime.now(timezone.utc)
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return max(0, int((end - started_at).total_seconds() * 1000))


def task_row(row: Mapping[str, Any], *, tier: str, now: Optional[datetime] = None) -> Dict[str, Any]:
    """One `task_journal` row as a `TaskRecord` dict for `tier`."""
    metadata = _mapping(row.get("metadata"))
    requestor = metadata.pop("requestor", None)
    location = metadata.pop("location", None)
    admin = tier == "admin"
    started_at = row.get("started_at")
    finished_at = row.get("finished_at")
    return {
        "id": str(row["id"]),
        "seq": int(row["seq"]),
        "kind": row["kind"],
        "cluster_id": row["cluster_id"],
        "project": row.get("project") or "default",
        "resource_type": _optional_str(row.get("resource_type")),
        "resource_name": _optional_str(row.get("resource_name")),
        "operation_id": _optional_str(row.get("operation_id")),
        "operation_class": _optional_str(row.get("operation_class")),
        "action": _optional_str(row.get("action")),
        "description": row.get("description") or "",
        "status": row["status"],
        "status_code": row.get("status_code"),
        "progress": _optional_str(row.get("progress")),
        "error": _optional_str(row.get("error")),
        "may_cancel": bool(row.get("may_cancel")),
        "actor_email": _optional_str(row.get("actor_email")),
        "actor_role": _optional_str(row.get("actor_role")),
        "actor_kind": row.get("actor_kind") or "system",
        "api_key_id": _optional_str(row.get("api_key_id")),
        "requestor": _requestor(requestor, tier),
        "source": row.get("source") or "events",
        "request_id": _optional_str(row.get("request_id")),
        "target_organization_id": row.get("target_organization_id") if admin else None,
        "target_organization_slug": _optional_str(row.get("target_organization_slug")) if admin else None,
        "location": _optional_str(location) if tier in ("admin", "partner") else None,
        "metadata": _tenant_cut(metadata) if tier == "tenant" else metadata,
        "lxd_created_at": row.get("lxd_created_at"),
        "lxd_updated_at": row.get("lxd_updated_at"),
        "started_at": started_at,
        "finished_at": finished_at,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "duration_ms": duration_ms(started_at, finished_at, now),
    }


def log_row(row: Mapping[str, Any], *, tier: str) -> Dict[str, Any]:
    """One `cluster_log` row as a `ClusterLogEntry` dict for `tier`."""
    details = row.get("details")
    presented: Optional[Dict[str, Any]] = _mapping(details) if isinstance(details, Mapping) else None
    if presented is not None and tier == "tenant":
        presented = _tenant_cut(presented)
    return {
        "id": int(row["id"]),
        "ts": row["ts"],
        "kind": row["kind"],
        "severity": row["severity"],
        "cluster_id": _optional_str(row.get("cluster_id")),
        "project": _optional_str(row.get("project")),
        "resource_type": _optional_str(row.get("resource_type")),
        "resource_name": _optional_str(row.get("resource_name")),
        "action": row["action"],
        "message": row.get("message") or "",
        "actor_email": _optional_str(row.get("actor_email")),
        "actor_role": _optional_str(row.get("actor_role")),
        "actor_kind": row.get("actor_kind") or "system",
        "api_key_id": _optional_str(row.get("api_key_id")),
        "result": _optional_str(row.get("result")),
        "request_id": _optional_str(row.get("request_id")),
        "event_id": _optional_str(row.get("event_id")),
        "task_id": _optional_str(row.get("task_id")),
        "target_organization_id": row.get("target_organization_id") if tier == "admin" else None,
        "details": presented,
    }
