"""The SQL fragments and bind builders every journal write shares.

Split out of `repository` so that module stays under the house limit; nothing here touches a
session. The fragments name the existing row by table (`task_journal.status`) so one text
serves a plain UPDATE and an ON CONFLICT DO UPDATE, and every bind that is read in more than
one type context carries an explicit cast (asyncpg deduces one type per parameter).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.services.task_journal.repository_types import (
    ActorFields,
    CorrelationKey,
    OperationFrame,
    clean_message,
    fit_params,
    json_param,
)

_RESULT_STATUS = {"accepted": "running", "success": "success", "failure": "failure"}

# important: asyncpg deduces one type per bind. `:new_status` is read in VALUES (a varchar
# column), in a CASE and in an IN list (text), so every use carries the same cast or Postgres
# refuses the statement with "inconsistent types deduced". Found on the first live seed.
_NEW = "CAST(:new_status AS varchar)"
_TERMINAL_3 = "('success', 'failure', 'cancelled')"

# important: the audit side never reopens or overrides a finished row, and `pending` never
# overrides `running`. The existing row is named by table so the same text serves a plain
# UPDATE and ON CONFLICT DO UPDATE.
MONOTONIC_STATUS = (
    "CASE WHEN task_journal.status IN ('success', 'failure', 'cancelled', 'lost') "
    "THEN task_journal.status "
    f"WHEN {_NEW} = 'pending' AND task_journal.status = 'running' THEN task_journal.status "
    f"ELSE {_NEW} END"
)

# important: the stream side is the cluster's own verdict. Only a stream frame writes
# `status_code`, and only a terminal frame writes one of 200 / 400 / 401, so a finished row
# carrying a code of 200 or more is final. A finished row with no code, or an open code (105
# Pending, 103 Running), was closed by the dashboard's audit (a `wait=True` router that timed
# out or answered early) and yields to the cluster's terminal frame, never to an open one.
# `lost` is the journal's own mark and yields to whatever the cluster reports next. Found on
# the scratch database: a Pending frame, the router's success, then the cluster's Failure
# left the row on success.
_CLUSTER_VERDICT_FINAL = (
    f"task_journal.status IN {_TERMINAL_3} AND task_journal.status_code >= 200"
)
_AUDIT_VERDICT_YIELDS = (
    f"task_journal.status IN {_TERMINAL_3} "
    "AND (task_journal.status_code IS NULL OR task_journal.status_code < 200) "
    f"AND {_NEW} IN {_TERMINAL_3}"
)
STREAM_STATUS = (
    f"CASE WHEN {_CLUSTER_VERDICT_FINAL} THEN task_journal.status "
    f"WHEN task_journal.status IN {_TERMINAL_3} AND {_NEW} NOT IN {_TERMINAL_3} "
    "THEN task_journal.status "
    f"WHEN {_NEW} = 'pending' AND task_journal.status = 'running' THEN task_journal.status "
    f"ELSE {_NEW} END"
)

_TERMINAL_FINISH = (
    "CASE WHEN CAST(:new_status AS varchar) IN ('success', 'failure', 'cancelled') "
    "THEN COALESCE(task_journal.finished_at, now()) ELSE task_journal.finished_at END"
)

# A stream frame's finish time is the daemon's last update, not the moment the journal saw it:
# a seeded operation that finished days ago would otherwise read as a days-long task.
_STREAM_FINISH = (
    f"CASE WHEN {_NEW} IN {_TERMINAL_3} "
    "THEN COALESCE(task_journal.finished_at, CAST(:lxd_updated_at AS timestamptz), now()) "
    "ELSE task_journal.finished_at END"
)

_OPERATION_SET = (
    f"status = {STREAM_STATUS}, "
    "status_code = COALESCE(CAST(:status_code AS integer), task_journal.status_code), "
    "progress = COALESCE(:progress, task_journal.progress), "
    # When the cluster's verdict replaces the audit's, its error text replaces the audit's too:
    # a Success frame after a timed-out wait leaves no error on the row (the audit line keeps it).
    f"error = CASE WHEN {_AUDIT_VERDICT_YIELDS} THEN NULLIF(:error, '') "
    "ELSE COALESCE(NULLIF(:error, ''), task_journal.error) END, "
    "may_cancel = :may_cancel, "
    "operation_class = COALESCE(:operation_class, task_journal.operation_class), "
    "description = COALESCE(NULLIF(CAST(:description AS varchar), ''), task_journal.description), "
    "lxd_updated_at = GREATEST("
    "COALESCE(task_journal.lxd_updated_at, CAST(:lxd_updated_at AS timestamptz)), "
    "CAST(:lxd_updated_at AS timestamptz)), "
    f"finished_at = {_STREAM_FINISH}, "
    "updated_at = now(), seq = nextval('task_journal_seq')"
)

# important: an older non-terminal frame and a duplicate frame are both no-ops with no seq
# bump; a terminal frame always lands, whatever its daemon clock says.
_OPERATION_GUARD = (
    "(task_journal.lxd_updated_at IS NULL OR CAST(:lxd_updated_at AS timestamptz) IS NULL "
    "OR CAST(:lxd_updated_at AS timestamptz) >= task_journal.lxd_updated_at "
    "OR CAST(:new_status AS varchar) IN ('success', 'failure', 'cancelled')) "
    "AND (task_journal.status IS DISTINCT FROM :effective "
    "OR task_journal.progress IS DISTINCT FROM :progress "
    "OR task_journal.error IS DISTINCT FROM NULLIF(:error, ''))"
)

# important: LXD names no resource on some operations ("Creating instance", "Deleting project"
# arrive with `resources: {}`), so the stream row has no key of its own. The audit record is
# the one writer that knows the name; it fills whatever the row lacks and never overwrites
# what the daemon did report. Found live: a create with no instance name and a project
# delete listed twice.
_KEY_BACKFILL = (
    "resource_type = COALESCE(task_journal.resource_type, :resource_type), "
    "resource_name = COALESCE(task_journal.resource_name, :resource_name), "
    "action = COALESCE(task_journal.action, :action), "
    "project = COALESCE(NULLIF(CASE WHEN task_journal.project = 'default' "
    "THEN CAST(:project AS varchar) END, ''), task_journal.project), "
)

_ACTOR_SET = (
    f"{_KEY_BACKFILL}"
    "actor_email = :actor_email, actor_role = :actor_role, actor_kind = :actor_kind, "
    "api_key_id = :api_key_id, source = :source, request_id = :request_id, "
    "target_organization_id = COALESCE(CAST(:target_organization_id AS integer), "
    "task_journal.target_organization_id), "
    "target_organization_slug = COALESCE(:target_organization_slug, "
    "task_journal.target_organization_slug), "
    "metadata = task_journal.metadata || CAST(:extra AS jsonb), "
    "error = COALESCE(task_journal.error, NULLIF(:audit_error, '')), "
    f"status = {MONOTONIC_STATUS}, "
    f"finished_at = {_TERMINAL_FINISH}, "
    "updated_at = now(), seq = nextval('task_journal_seq')"
)

_ON_CONFLICT = "ON CONFLICT (cluster_id, operation_id) WHERE operation_id IS NOT NULL"

_KEY_MATCH = (
    "cluster_id = :cluster_id AND project = :project AND resource_type = :resource_type "
    "AND resource_name = :resource_name AND action = :action"
)


def _effective(current: str, new: str, *, cluster_verdict: bool) -> str:
    """STREAM_STATUS in Python, for the change guard: the row is touched only if this differs."""
    if current in ("success", "failure", "cancelled"):
        if cluster_verdict or new not in ("success", "failure", "cancelled"):
            return current
        return new
    if new == "pending" and current == "running":
        return current
    return new


def _status_for(result: str) -> str:
    return _RESULT_STATUS.get(result, "running")


def _require_operation_id(operation_id: Optional[str]) -> str:
    # important: the unique index is partial on `operation_id IS NOT NULL`, so an empty string
    # would be one shared key for every row that carried it. Refused before any SQL.
    if not isinstance(operation_id, str) or not operation_id.strip():
        raise ValueError("operation_id must be a non-blank string")
    return operation_id.strip()


def _actor_params(actor: ActorFields) -> Dict[str, Any]:
    return fit_params({
        "actor_email": actor.actor_email,
        "actor_role": actor.actor_role,
        "actor_kind": actor.actor_kind,
        "api_key_id": actor.api_key_id,
        "source": actor.source,
        "request_id": actor.request_id,
        "target_organization_id": actor.target_organization_id,
        "target_organization_slug": actor.target_organization_slug,
    })


def _key_params(key: CorrelationKey) -> Dict[str, Any]:
    return fit_params({
        "cluster_id": key.cluster_id,
        "project": key.project,
        "resource_type": key.resource_type,
        "resource_name": key.resource_name,
        "action": key.action,
    })


def _frame_params(frame: OperationFrame, operation_id: str) -> Dict[str, Any]:
    return fit_params({
        "cluster_id": frame.cluster_id,
        "operation_id": operation_id,
        "operation_class": frame.operation_class,
        "description": frame.description or "",
        "new_status": frame.status,
        "status_code": frame.status_code,
        "progress": frame.progress,
        "error": clean_message(frame.error) or "",
        "may_cancel": bool(frame.may_cancel),
        "project": frame.project or "default",
        "resource_type": frame.resource_type,
        "resource_name": frame.resource_name,
        "action": frame.action,
        "lxd_created_at": frame.lxd_created_at,
        "lxd_updated_at": frame.lxd_updated_at,
        "metadata": json_param(frame.metadata),
        "actor_kind": frame.actor_kind,
        "source": frame.source,
        "actor_email": frame.actor_email,
    })
