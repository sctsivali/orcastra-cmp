"""Hub frames into repository writes: pure functions the follower's writer and the seed share.

An `operation` frame from the events hub and an operation read from `/1.0/operations` land
here in the same shape, so one mapping decides the journal status, the action, the resource
and what little metadata is kept. A `lifecycle` frame becomes one cluster log row and the
correlation key that lets its `requestor` name the actor of the operation it belongs to.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, Mapping, Optional, Tuple

from app.core.error_sanitizer import sanitize_error
from app.services.task_journal.repository_types import CorrelationKey, OperationFrame
from app.services.task_journal.vocabulary import (
    DESCRIPTION_TO_ACTION,
    LIFECYCLE_TO_ACTION,
    OPEN,
    REQUESTOR_KEYS,
    allowlisted_metadata,
    bounded_json,
    lifecycle_message,
    lifecycle_resource_type,
    parse_lxd_time,
    project_from_operation,
    requestor_kind,
    resource_from_operation,
    status_from_lxd,
)

logger = logging.getLogger(__name__)

# What the seed and the follower name themselves in the log rows they write directly.
UPSTREAM_LOST_ACTION = "journal.upstream.lost"
UPSTREAM_RESTORED_ACTION = "journal.upstream.restored"


def _requestor(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, Mapping):
        return None
    picked = {k: value.get(k) for k in REQUESTOR_KEYS if value.get(k) is not None}
    return picked or None


def operation_frame(
    payload: Mapping[str, Any], dashboard_identities: frozenset = frozenset()
) -> Optional[OperationFrame]:
    """The repository frame for one hub `operation` payload; None for a blank operation id.

    The requestor names the actor the same way a lifecycle frame does, so an operation that
    only reaches the journal through the seed (an `lxc exec` from the host's shell, an
    operation the stream missed while the follower was down) still reads as CLI work.

    The hub has already stripped `?project=` from every resource name and never carries the
    inner metadata, so nothing here can see a file descriptor or an exec command line. A
    websocket-class operation keeps its action (`instance.exec` / `instance.console`) and its
    location and resources, and nothing else.
    """
    operation_id = str(payload.get("operation_id") or "").strip()
    host = str(payload.get("host") or "")
    if not operation_id or not host:
        logger.debug("operation frame without an id or host skipped: %r", payload.get("description"))
        return None
    description = str(payload.get("description") or "")
    resources = payload.get("resources")
    resource_type, resource_name = resource_from_operation(resources)
    project = payload.get("project") or project_from_operation(resources) or "default"
    err = payload.get("err")
    requestor = _requestor(payload.get("requestor"))
    metadata = allowlisted_metadata(
        {
            "location": payload.get("location"),
            "resources": resources if isinstance(resources, Mapping) else None,
            "requestor": requestor,
        }
    )
    status_code = payload.get("status_code")
    actor_kind, source = requestor_kind(requestor, dashboard_identities)
    actor_email = None
    if actor_kind == "external" and requestor and requestor.get("username"):
        actor_email = str(requestor["username"])
    return OperationFrame(
        cluster_id=host,
        operation_id=operation_id,
        operation_class=payload.get("class"),
        description=description,
        status=status_from_lxd(payload.get("status"), status_code),
        status_code=int(status_code) if isinstance(status_code, int) else None,
        progress=payload.get("progress") if isinstance(payload.get("progress"), str) else None,
        error=sanitize_error(str(err)) if err else None,
        may_cancel=bool(payload.get("may_cancel")),
        project=str(project),
        resource_type=resource_type,
        resource_name=resource_name,
        action=DESCRIPTION_TO_ACTION.get(description),
        lxd_created_at=parse_lxd_time(payload.get("created_at")),
        lxd_updated_at=parse_lxd_time(payload.get("updated_at")),
        metadata=metadata,
        requestor=requestor,
        actor_kind=actor_kind,
        source=source,
        actor_email=actor_email,
    )


def is_progress_only(payload: Mapping[str, Any]) -> bool:
    """An open operation frame carrying a progress string: the first thing dropped under load."""
    if payload.get("type") != "operation":
        return False
    status = status_from_lxd(payload.get("status"), payload.get("status_code"))
    return status in OPEN and bool(payload.get("progress"))


def lifecycle_event_id(host: str, payload: Mapping[str, Any]) -> Optional[str]:
    """A stable id for a lifecycle frame, so a hand-over between two followers writes it once."""
    stamp = payload.get("timestamp")
    if not stamp:
        return None
    digest = hashlib.sha256(
        "|".join(
            str(payload.get(k) or "") for k in ("action", "name", "project")
        ).encode("utf-8")
        + b"|"
        + str(stamp).encode("utf-8")
    ).hexdigest()[:32]
    return f"lifecycle:{host}:{digest}"


def lifecycle_row(
    host: str, payload: Mapping[str, Any], dashboard_identities: frozenset
) -> Tuple[Dict[str, Any], Optional[CorrelationKey], Tuple[str, str, Optional[str]]]:
    """(log row, correlation key or None, (actor_kind, source, actor_email)) for one frame.

    The row names the requestor the daemon reported. A unix-socket caller is someone at an
    `lxc` shell and becomes an external actor with the username LXD saw; the dashboard's own
    certificate is a system actor whose person the audit record supplies.
    """
    action = str(payload.get("action") or "")
    name = payload.get("name")
    name_text = str(name) if name else None
    project = str(payload.get("project") or "default")
    requestor = _requestor(payload.get("requestor"))
    actor_kind, source = requestor_kind(requestor, dashboard_identities)
    actor_email = None
    if actor_kind == "external" and requestor and requestor.get("username"):
        actor_email = str(requestor["username"])
    details: Dict[str, Any] = {"source": payload.get("source")}
    if requestor:
        details["requestor"] = requestor
    context = payload.get("context")
    if isinstance(context, Mapping) and context:
        details["context"] = dict(context)
    resource_type = lifecycle_resource_type(action)
    row = {
        "ts": None,
        "kind": "lifecycle",
        "severity": "info",
        "cluster_id": host,
        "project": project,
        "resource_type": resource_type,
        "resource_name": name_text,
        "action": action,
        "message": lifecycle_message(action, name_text),
        "actor_email": actor_email,
        "actor_role": None,
        "actor_kind": actor_kind,
        "api_key_id": None,
        "result": None,
        "request_id": None,
        "event_id": lifecycle_event_id(host, payload),
        "task_id": None,
        "target_organization_id": None,
        "details": bounded_json(details),
    }
    mapped = LIFECYCLE_TO_ACTION.get(action)
    key = None
    if mapped and name_text:
        key = CorrelationKey(
            cluster_id=host,
            project=project,
            resource_type=resource_type,
            resource_name=name_text,
            action=mapped,
        )
    return row, key, (actor_kind, source, actor_email)


def upstream_row(host: str, state: str, error: Optional[str]) -> Dict[str, Any]:
    """The cluster log line the follower writes when a cluster's event stream comes or goes."""
    lost = state != "connected"
    message = (
        f"Lost the event stream from {host}" if lost else f"Following the event stream from {host}"
    )
    if lost and error:
        message = f"{message}: {sanitize_error(str(error))}"
    return {
        "ts": None,
        "kind": "task",
        "severity": "warning" if lost else "info",
        "cluster_id": host,
        "project": None,
        "resource_type": "cluster",
        "resource_name": host,
        "action": UPSTREAM_LOST_ACTION if lost else UPSTREAM_RESTORED_ACTION,
        "message": message,
        "actor_email": None,
        "actor_role": None,
        "actor_kind": "system",
        "api_key_id": None,
        "result": None,
        "request_id": None,
        "event_id": None,
        "task_id": None,
        "target_organization_id": None,
        "details": {"state": state},
    }
