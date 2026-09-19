"""Every write the task journal makes, as raw SQL against the session the caller passes.

Two writers on four workers feed the same tables: the events follower (one elected worker) and
the activity sink (every worker). No write here can regress a terminal status, every claim goes
through `LIMIT 1 FOR UPDATE SKIP LOCKED`, and every insert keyed on an LXD operation carries
`ON CONFLICT`, so a hand-over overlap between two followers or an audit record landing before
or after its frame is safe in either order. Nothing here commits; the caller owns the
transaction. Nothing here names a scope helper: rows are written as facts, and scope is applied
on read.

This module is the package's public write API: the sweeps and the follower's cluster-state
helpers live in `repository_sweeps`, the cluster log rows in `repository_log`; both are
re-exported here.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.task_journal.repository_log import (  # noqa: F401 - public re-exports
    insert_log_rows,
    link_log_to_task,
)
from app.services.task_journal.repository_sweeps import (  # noqa: F401 - public re-exports
    expire_stale_actions,
    follower_is_active,
    mark_lost,
    merge_racing_pairs,
    prune,
    read_cluster_states,
    reset_cluster_states,
    set_cluster_state,
    touch_cluster_frame,
)
from app.services.task_journal.repository_types import (  # noqa: F401 - public re-exports
    FOLLOWER_LOCK,
    PRUNE_LOCK,
    ActorFields,
    CorrelationKey,
    OperationFrame,
    UpsertResult,
    clean_message,
    json_param,
    returned_id,
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
    return {
        "actor_email": actor.actor_email,
        "actor_role": actor.actor_role,
        "actor_kind": actor.actor_kind,
        "api_key_id": actor.api_key_id,
        "source": actor.source,
        "request_id": actor.request_id,
        "target_organization_id": actor.target_organization_id,
        "target_organization_slug": actor.target_organization_slug,
    }


def _key_params(key: CorrelationKey) -> Dict[str, Any]:
    return {
        "cluster_id": key.cluster_id,
        "project": key.project,
        "resource_type": key.resource_type,
        "resource_name": key.resource_name,
        "action": key.action,
    }


def _frame_params(frame: OperationFrame, operation_id: str) -> Dict[str, Any]:
    return {
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
    }


async def upsert_operation(db: AsyncSession, frame: OperationFrame) -> UpsertResult:
    operation_id = _require_operation_id(frame.operation_id)
    params = _frame_params(frame, operation_id)

    found = (
        await db.execute(
            text(
                "SELECT id, status, lxd_updated_at, status_code FROM task_journal "
                "WHERE cluster_id = :cluster_id AND operation_id = :operation_id FOR UPDATE"
            ),
            {"cluster_id": frame.cluster_id, "operation_id": operation_id},
        )
    ).first()
    if found is not None:
        row_id, current = str(found[0]), str(found[1])
        params["effective"] = _effective(
            current, frame.status, cluster_verdict=(found[3] or 0) >= 200
        )
        params["id"] = row_id
        result = await db.execute(
            text(
                f"UPDATE task_journal SET {_OPERATION_SET} "
                f"WHERE id = CAST(:id AS uuid) AND {_OPERATION_GUARD}"
            ),
            params,
        )
        return UpsertResult(id=row_id, created=False, changed=result.rowcount == 1)

    if frame.resource_type and frame.resource_name and frame.action:
        # A dashboard row written by the audit sink for the same key within the window is
        # this operation: the frame attaches to it, whatever its status, because a `wait=True`
        # action's row is already terminal when its frame arrives.
        attached = returned_id(
            await db.execute(
                text(
                    "UPDATE task_journal SET operation_id = :operation_id, "
                    "kind = CASE WHEN task_journal.kind = 'dashboard_action' "
                    "THEN 'lxd_operation' ELSE task_journal.kind END, "
                    "operation_class = :operation_class, "
                    f"status = {STREAM_STATUS}, "
                    "status_code = CAST(:status_code AS integer), progress = :progress, "
                    "may_cancel = :may_cancel, "
                    "lxd_created_at = CAST(:lxd_created_at AS timestamptz), "
                    "lxd_updated_at = CAST(:lxd_updated_at AS timestamptz), "
                    "started_at = COALESCE(task_journal.started_at, "
                    "CAST(:lxd_created_at AS timestamptz)), "
                    "description = COALESCE(NULLIF(task_journal.description, ''), CAST(:description AS varchar)), "
                    "metadata = task_journal.metadata || CAST(:metadata AS jsonb), "
                    "seq = nextval('task_journal_seq'), updated_at = now() "
                    "WHERE id = (SELECT id FROM task_journal WHERE "
                    f"{_KEY_MATCH} AND operation_id IS NULL "
                    "AND created_at >= now() - interval '15 minutes' "
                    "ORDER BY created_at DESC LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING id"
                ),
                params,
            )
        )
        if attached is not None:
            return UpsertResult(id=attached, created=False, changed=True)

    params["effective"] = None
    inserted = (
        await db.execute(
            text(
                "INSERT INTO task_journal (kind, cluster_id, project, resource_type, "
                "resource_name, operation_id, operation_class, action, description, status, "
                "status_code, progress, error, may_cancel, actor_kind, source, actor_email, "
                "metadata, lxd_created_at, lxd_updated_at, started_at, finished_at) VALUES ("
                "'lxd_operation', :cluster_id, :project, :resource_type, :resource_name, "
                ":operation_id, :operation_class, :action, CAST(:description AS varchar), CAST(:new_status AS varchar), "
                "CAST(:status_code AS integer), :progress, NULLIF(:error, ''), :may_cancel, "
                ":actor_kind, :source, :actor_email, CAST(:metadata AS jsonb), "
                "CAST(:lxd_created_at AS timestamptz), CAST(:lxd_updated_at AS timestamptz), "
                "COALESCE(CAST(:lxd_created_at AS timestamptz), now()), "
                "CASE WHEN CAST(:new_status AS varchar) IN ('success', 'failure', 'cancelled') "
                "THEN COALESCE(CAST(:lxd_updated_at AS timestamptz), now()) END) "
                f"{_ON_CONFLICT} DO UPDATE SET {_OPERATION_SET} WHERE {_OPERATION_GUARD} "
                "RETURNING id, (xmax = 0) AS inserted"
            ),
            params,
        )
    ).first()
    if inserted is not None:
        return UpsertResult(id=str(inserted[0]), created=bool(inserted[1]), changed=True)
    # The conflict update matched nothing: the row exists and this frame changes nothing.
    existing = (
        await db.execute(
            text(
                "SELECT id FROM task_journal "
                "WHERE cluster_id = :cluster_id AND operation_id = :operation_id"
            ),
            {"cluster_id": frame.cluster_id, "operation_id": operation_id},
        )
    ).first()
    return UpsertResult(id=str(existing[0]) if existing else "", created=False, changed=False)


async def attach_operation_by_id(
    db: AsyncSession,
    *,
    cluster_id: str,
    operation_id: str,
    kind: str,
    key: CorrelationKey,
    actor: ActorFields,
    result: str,
    description: str,
    audit_error: Optional[str],
    extra: Dict[str, Any],
) -> str:
    operation_id = _require_operation_id(operation_id)
    params = {
        **_actor_params(actor),
        **_key_params(key),
        "cluster_id": cluster_id,
        "operation_id": operation_id,
        "kind": kind,
        "new_status": _status_for(result),
        "description": description or "",
        "audit_error": clean_message(audit_error) or "",
        "extra": json_param(extra),
    }
    found = (
        await db.execute(
            text(
                "SELECT id FROM task_journal "
                "WHERE cluster_id = :cluster_id AND operation_id = :operation_id FOR UPDATE"
            ),
            {"cluster_id": cluster_id, "operation_id": operation_id},
        )
    ).first()
    if found is not None:
        params["id"] = str(found[0])
        await db.execute(
            text(f"UPDATE task_journal SET {_ACTOR_SET} WHERE id = CAST(:id AS uuid)"), params
        )
        return str(found[0])

    # The audit record landed before the frame: the row is opened here and the frame's
    # upsert finds it by operation id. Either order ends with one row.
    attached = returned_id(
        await db.execute(
            text(
                "INSERT INTO task_journal (kind, cluster_id, project, resource_type, "
                "resource_name, operation_id, action, description, status, error, actor_email, "
                "actor_role, actor_kind, api_key_id, source, request_id, "
                "target_organization_id, target_organization_slug, metadata, started_at, "
                "finished_at) VALUES (:kind, :cluster_id, :project, :resource_type, "
                ":resource_name, :operation_id, :action, CAST(:description AS varchar), CAST(:new_status AS varchar), "
                "NULLIF(:audit_error, ''), :actor_email, :actor_role, :actor_kind, :api_key_id, "
                ":source, :request_id, CAST(:target_organization_id AS integer), "
                ":target_organization_slug, CAST(:extra AS jsonb), now(), "
                "CASE WHEN CAST(:new_status AS varchar) IN ('success', 'failure') THEN now() END) "
                f"{_ON_CONFLICT} DO UPDATE SET {_ACTOR_SET} RETURNING id"
            ),
            params,
        )
    )
    return attached or ""


async def claim_operation_for_audit(
    db: AsyncSession,
    *,
    key: CorrelationKey,
    actor: ActorFields,
    result: str,
    audit_error: Optional[str],
    extra: Dict[str, Any],
) -> Optional[str]:
    params = {
        **_actor_params(actor),
        **_key_params(key),
        "new_status": _status_for(result),
        "audit_error": clean_message(audit_error) or "",
        "extra": json_param(extra),
    }
    claimed = returned_id(
        await db.execute(
            text(
                f"UPDATE task_journal SET {_ACTOR_SET} "
                "WHERE id = (SELECT id FROM task_journal WHERE "
                f"{_KEY_MATCH} AND actor_email IS NULL AND api_key_id IS NULL "
                "AND source IN ('events', 'cli') "
                "AND created_at >= now() - interval '15 minutes' "
                "ORDER BY created_at DESC LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING id"
            ),
            params,
        )
    )
    if claimed:
        return claimed
    # The daemon named no resource on the operation: the newest unnamed stream row for the
    # same action on the same cluster inside the window is this action, and the key it
    # lacked is written now. Two operators running the same unnamed action on one cluster
    # inside fifteen minutes can be attached to the wrong actor; the stdout audit stays the
    # authority for that question, as with the key match above.
    return returned_id(
        await db.execute(
            text(
                f"UPDATE task_journal SET {_ACTOR_SET} "
                "WHERE id = (SELECT id FROM task_journal WHERE "
                "cluster_id = :cluster_id AND action = :action AND resource_name IS NULL "
                "AND operation_id IS NOT NULL AND actor_email IS NULL AND api_key_id IS NULL "
                "AND source IN ('events', 'cli') "
                "AND created_at >= now() - interval '15 minutes' "
                "ORDER BY created_at DESC LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING id"
            ),
            params,
        )
    )


async def insert_action(
    db: AsyncSession,
    *,
    kind: str,
    key: CorrelationKey,
    actor: ActorFields,
    result: str,
    duration_ms: Optional[float],
    description: str,
    audit_error: Optional[str],
    extra: Dict[str, Any],
    session_key: Optional[str] = None,
) -> str:
    metadata = dict(extra or {})
    if session_key:
        metadata["session_id"] = session_key
    params = {
        **_actor_params(actor),
        **_key_params(key),
        "kind": kind,
        "new_status": _status_for(result),
        "description": description or "",
        "audit_error": clean_message(audit_error) or "",
        "extra": json_param(metadata),
        "duration_ms": float(duration_ms) if duration_ms else None,
    }
    created = returned_id(
        await db.execute(
            text(
                "INSERT INTO task_journal (kind, cluster_id, project, resource_type, "
                "resource_name, action, description, status, error, actor_email, actor_role, "
                "actor_kind, api_key_id, source, request_id, target_organization_id, "
                "target_organization_slug, metadata, started_at, finished_at) VALUES ("
                ":kind, :cluster_id, :project, :resource_type, :resource_name, :action, "
                "CAST(:description AS varchar), CAST(:new_status AS varchar), NULLIF(:audit_error, ''), :actor_email, "
                ":actor_role, :actor_kind, :api_key_id, :source, :request_id, "
                "CAST(:target_organization_id AS integer), :target_organization_slug, "
                "CAST(:extra AS jsonb), "
                "now() - make_interval(secs => COALESCE(CAST(:duration_ms AS float8), 0) / 1000.0), "
                "CASE WHEN CAST(:new_status AS varchar) IN ('success', 'failure') THEN now() END) RETURNING id"
            ),
            params,
        )
    )
    return created or ""


async def update_action_by_session(
    db: AsyncSession, *, session_key: str, result: str, audit_error: Optional[str]
) -> Optional[str]:
    if not session_key:
        return None
    return returned_id(
        await db.execute(
            text(
                f"UPDATE task_journal SET status = {MONOTONIC_STATUS}, "
                "error = COALESCE(task_journal.error, NULLIF(:audit_error, '')), "
                f"finished_at = {_TERMINAL_FINISH}, "
                "updated_at = now(), seq = nextval('task_journal_seq') "
                "WHERE id = (SELECT id FROM task_journal WHERE kind = 'upload' "
                "AND metadata ->> 'session_id' = :session_key "
                "ORDER BY created_at DESC LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING id"
            ),
            {
                "session_key": session_key,
                "new_status": _status_for(result),
                "audit_error": clean_message(audit_error) or "",
            },
        )
    )


async def attach_requestor(
    db: AsyncSession,
    *,
    key: CorrelationKey,
    requestor: Dict[str, Any],
    actor_kind: str,
    source: str,
    actor_email: Optional[str],
    window_s: int = 120,
) -> Optional[str]:
    return returned_id(
        await db.execute(
            text(
                "UPDATE task_journal SET metadata = task_journal.metadata || "
                "jsonb_build_object('requestor', CAST(:requestor AS jsonb)), "
                "actor_kind = :actor_kind, source = :source, "
                "actor_email = COALESCE(task_journal.actor_email, :actor_email), "
                "updated_at = now(), seq = nextval('task_journal_seq') "
                "WHERE id = (SELECT id FROM task_journal WHERE "
                f"{_KEY_MATCH} AND kind = 'lxd_operation' AND actor_email IS NULL "
                "AND api_key_id IS NULL "
                "AND created_at >= now() - make_interval(secs => CAST(:window_s AS float8)) "
                "ORDER BY created_at DESC LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING id"
            ),
            {
                **_key_params(key),
                "requestor": json_param(requestor),
                "actor_kind": actor_kind,
                "source": source,
                "actor_email": actor_email,
                "window_s": float(window_s),
            },
        )
    )


