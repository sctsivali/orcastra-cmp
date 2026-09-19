"""From one audit record to the rows the journal keeps: pure functions, no I/O.

`record_to_log_row` decides whether a record is a cluster log line and how it reads;
`record_to_task` decides whether it is also a task, and which repository call opens or closes
that task. Both are total: an unexpected record shape yields None, never an exception, because
the sink runs behind every request and a mapping failure must cost nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

from app.core.error_sanitizer import sanitize_error
from app.services.activity_logger import HIGH_SEVERITY_ACTIONS
from app.services.task_journal.repository_types import ActorFields, CorrelationKey
from app.services.task_journal.vocabulary import (
    ACTION_DESCRIPTIONS,
    LOG_SUPPRESSED_ACTIONS,
    SUPPRESSED_ACTIONS,
    canonical_task_action,
    humanise_action,
    TARGET_TYPE_TO_RESOURCE,
    TASK_ACTIONS,
    allowlisted_metadata,
    bounded_json,
)

# Keys that never reach the journal whatever their value. `_sanitize_details` redacts values
# under credential-shaped names but keeps the names; here the name goes too, and the keys that
# carry command lines, file descriptors and before/after snapshots are dropped outright.
_DROPPED_KEYS = frozenset({"fds", "command", "environment", "before", "after", "args", "env"})
_SECRET_FRAGMENTS = (
    "password", "secret", "token", "api_key", "private_key", "credential", "cookie",
    "authorization", "passphrase",
)
_PUBLIC_KEYS = frozenset({"api_key_id"})
_MESSAGE_LIMIT = 512
_UPLOAD_KIND = "upload"

MODE_ATTACH_BY_ID = "attach_by_id"
MODE_CLAIM_OR_INSERT = "claim_or_insert"
MODE_UPLOAD_OPEN = "upload_open"
MODE_UPLOAD_CLOSE = "upload_close"


@dataclass(frozen=True)
class TaskWrite:
    mode: str
    kind: str
    key: CorrelationKey
    actor: ActorFields
    result: str
    description: str
    operation_id: Optional[str] = None
    session_key: Optional[str] = None
    duration_ms: Optional[float] = None
    audit_error: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


def _mapping(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    out = str(value).strip()
    return out or None


def scrub(details: Any) -> Any:
    """Drop every key that must never be stored, recursively, keeping public identifiers."""
    if isinstance(details, Mapping):
        out: Dict[str, Any] = {}
        for key, value in details.items():
            name = str(key)
            lowered = name.lower()
            if lowered in _DROPPED_KEYS:
                continue
            if lowered not in _PUBLIC_KEYS and any(f in lowered for f in _SECRET_FRAGMENTS):
                continue
            out[name] = scrub(value)
        return out
    if isinstance(details, (list, tuple)):
        return [scrub(v) for v in details]
    return details


def actor_kind_for(record: Mapping[str, Any]) -> str:
    action = str(record.get("action") or "")
    details = _mapping(record.get("details"))
    if details.get("api_key_id") or action.startswith("integration."):
        return "api_key"
    if _mapping(record.get("actor")).get("user_id"):
        return "user"
    return "system"


def source_for(record: Mapping[str, Any]) -> str:
    action = str(record.get("action") or "")
    if action == "integration.mcp.tool_call":
        return "mcp"
    if action.startswith("integration."):
        return "integration"
    if _mapping(record.get("actor")).get("role") == "scheduler":
        return "schedule"
    return "dashboard"


def severity_for(record: Mapping[str, Any]) -> str:
    result = record.get("result")
    if result == "failure":
        return "error"
    if result == "denied" or record.get("action") in HIGH_SEVERITY_ACTIONS:
        return "notice"
    return "info"


def actor_fields(record: Mapping[str, Any]) -> ActorFields:
    actor = _mapping(record.get("actor"))
    target = _mapping(record.get("target"))
    details = _mapping(record.get("details"))
    user_id = _text(actor.get("user_id"))
    org_id = target.get("organization_id")
    return ActorFields(
        actor_email=user_id if user_id and "@" in user_id else None,
        actor_role=_text(actor.get("role")),
        actor_kind=actor_kind_for(record),
        api_key_id=_text(details.get("api_key_id")),
        source=source_for(record),
        request_id=_text(record.get("request_id")),
        target_organization_id=int(org_id) if isinstance(org_id, int) else None,
        target_organization_slug=_text(target.get("organization_slug")),
    )


def _resource_type(target: Mapping[str, Any]) -> Optional[str]:
    raw = _text(target.get("type"))
    if raw is None:
        return None
    return TARGET_TYPE_TO_RESOURCE.get(raw, raw)


def _message(record: Mapping[str, Any], action: str, target: Mapping[str, Any]) -> str:
    verb = ACTION_DESCRIPTIONS.get(action) or humanise_action(action)
    subject = _text(target.get("id"))
    message = f"{verb} {subject}" if subject else verb
    if record.get("result") == "failure":
        error = _text(record.get("error_message")) or _text(record.get("error_code"))
        if error:
            message = f"{message}: {sanitize_error(error)}"
    elif record.get("result") == "denied":
        message = f"{message}: denied"
    return sanitize_error(message, limit=_MESSAGE_LIMIT)


def record_to_log_row(record: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(record, Mapping):
        return None
    action = _text(record.get("action"))
    if action is None or action in SUPPRESSED_ACTIONS or action in LOG_SUPPRESSED_ACTIONS:
        return None
    target = _mapping(record.get("target"))
    details = _mapping(record.get("details"))
    actor = actor_fields(record)
    return {
        "ts": _text(record.get("@timestamp")),
        "kind": "audit",
        "severity": severity_for(record),
        "cluster_id": _text(target.get("host")),
        "project": _text(target.get("project")) or _text(details.get("project")),
        "resource_type": _resource_type(target),
        "resource_name": _text(target.get("id")),
        "action": action,
        "message": _message(record, action, target),
        "actor_email": actor.actor_email,
        "actor_role": actor.actor_role,
        "actor_kind": actor.actor_kind,
        "api_key_id": actor.api_key_id,
        "result": _text(record.get("result")),
        "request_id": actor.request_id,
        "event_id": _text(record.get("event_id")),
        "task_id": None,
        "target_organization_id": actor.target_organization_id,
        "details": bounded_json(scrub(details)) if details else None,
    }


def _task_extra(record: Mapping[str, Any], details: Mapping[str, Any]) -> Dict[str, Any]:
    extra: Dict[str, Any] = {}
    for key in ("job_id", "session_id", "snapshot"):
        if details.get(key) is not None:
            extra[key] = details[key]
    actor_extra = _mapping(record.get("actor_extra"))
    if actor_extra.get("schedule_id") is not None:
        extra["schedule_id"] = actor_extra["schedule_id"]
    return allowlisted_metadata(scrub(extra))


def record_to_task(record: Mapping[str, Any]) -> Optional[TaskWrite]:
    if not isinstance(record, Mapping):
        return None
    action = _text(record.get("action"))
    result = _text(record.get("result")) or "success"
    if action is None or action in SUPPRESSED_ACTIONS or action not in TASK_ACTIONS:
        return None
    if result == "denied":
        return None
    kind, resource_type = TASK_ACTIONS[action]
    # The key and the description use the canonical action; the log row keeps the emitted one.
    action = canonical_task_action(action)
    target = _mapping(record.get("target"))
    details = _mapping(record.get("details"))

    cluster_id = _text(target.get("host"))
    project = _text(target.get("project")) or _text(details.get("project")) or "default"
    resource_name = _text(target.get("id"))
    # A cross-cluster move is driven by the destination, so its operation, and the task, live
    # on the target cluster under the target's name and project.
    if action == "instance.migrate" and details.get("transport") != "copy":
        target_host = _text(details.get("target_host"))
        if target_host:
            cluster_id = target_host
            project = _text(details.get("target_project")) or project
            resource_name = _text(details.get("target_name")) or resource_name
    if cluster_id is None or resource_name is None:
        return None

    key = CorrelationKey(
        cluster_id=cluster_id,
        project=project,
        resource_type=resource_type,
        resource_name=resource_name,
        action=action,
    )
    metadata = _mapping(record.get("metadata"))
    duration = metadata.get("duration_ms")
    audit_error = None
    if result == "failure":
        audit_error = _text(record.get("error_message")) or _text(record.get("error_code"))
    common = {
        "kind": kind,
        "key": key,
        "actor": actor_fields(record),
        "result": result,
        "description": ACTION_DESCRIPTIONS.get(action, action),
        "duration_ms": float(duration) if isinstance(duration, (int, float)) else None,
        "audit_error": sanitize_error(audit_error) if audit_error else None,
        "extra": _task_extra(record, details),
    }

    operation_id = _text(details.get("operation_id"))
    if operation_id:
        return TaskWrite(mode=MODE_ATTACH_BY_ID, operation_id=operation_id, **common)
    if kind == _UPLOAD_KIND:
        session_key = _text(details.get("session_id"))
        if session_key is None:
            return TaskWrite(mode=MODE_CLAIM_OR_INSERT, **common)
        mode = MODE_UPLOAD_OPEN if result == "accepted" else MODE_UPLOAD_CLOSE
        return TaskWrite(mode=mode, session_key=session_key, **common)
    return TaskWrite(mode=MODE_CLAIM_OR_INSERT, **common)
