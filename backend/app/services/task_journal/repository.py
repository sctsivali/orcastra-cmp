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
from app.services.task_journal.vocabulary import TERMINAL

_RESULT_STATUS = {"accepted": "running", "success": "success", "failure": "failure"}

# important: a terminal status is final and `pending` never overrides `running`. The existing
# row is named by table so the same text serves a plain UPDATE and ON CONFLICT DO UPDATE.
MONOTONIC_STATUS = (
    "CASE WHEN task_journal.status IN ('success', 'failure', 'cancelled', 'lost') "
    "THEN task_journal.status "
    "WHEN :new_status = 'pending' AND task_journal.status = 'running' THEN task_journal.status "
    "ELSE :new_status END"
)

_TERMINAL_FINISH = (
    "CASE WHEN :new_status IN ('success', 'failure', 'cancelled') "
    "THEN COALESCE(task_journal.finished_at, now()) ELSE task_journal.finished_at END"
)

_OPERATION_SET = (
    f"status = {MONOTONIC_STATUS}, "
    "status_code = COALESCE(CAST(:status_code AS integer), task_journal.status_code), "
    "progress = COALESCE(:progress, task_journal.progress), "
    "error = COALESCE(NULLIF(:error, ''), task_journal.error), "
    "may_cancel = :may_cancel, "
    "operation_class = COALESCE(:operation_class, task_journal.operation_class), "
    "description = COALESCE(NULLIF(:description, ''), task_journal.description), "
    "lxd_updated_at = GREATEST("
    "COALESCE(task_journal.lxd_updated_at, CAST(:lxd_updated_at AS timestamptz)), "
    "CAST(:lxd_updated_at AS timestamptz)), "
    f"finished_at = {_TERMINAL_FINISH}, "
    "updated_at = now(), seq = nextval('task_journal_seq')"
)

# important: an older non-terminal frame and a duplicate frame are both no-ops with no seq
# bump; a terminal frame always lands, whatever its daemon clock says.
_OPERATION_GUARD = (
    "(task_journal.lxd_updated_at IS NULL OR CAST(:lxd_updated_at AS timestamptz) IS NULL "
    "OR CAST(:lxd_updated_at AS timestamptz) >= task_journal.lxd_updated_at "
    "OR :new_status IN ('success', 'failure', 'cancelled')) "
    "AND (task_journal.status IS DISTINCT FROM :effective "
    "OR task_journal.progress IS DISTINCT FROM :progress "
    "OR task_journal.error IS DISTINCT FROM NULLIF(:error, ''))"
)

_ACTOR_SET = (
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


def _effective(current: str, new: str) -> str:
    if current in TERMINAL:
        return current
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
    }


async def upsert_operation(db: AsyncSession, frame: OperationFrame) -> UpsertResult:
    operation_id = _require_operation_id(frame.operation_id)
    params = _frame_params(frame, operation_id)

    found = (
        await db.execute(
            text(
                "SELECT id, status, lxd_updated_at FROM task_journal "
                "WHERE cluster_id = :cluster_id AND operation_id = :operation_id FOR UPDATE"
            ),
            {"cluster_id": frame.cluster_id, "operation_id": operation_id},
        )
    ).first()
    if found is not None:
        row_id, current = str(found[0]), str(found[1])
        params["effective"] = _effective(current, frame.status)
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
                    f"status = {MONOTONIC_STATUS}, "
                    "status_code = CAST(:status_code AS integer), progress = :progress, "
                    "may_cancel = :may_cancel, "
                    "lxd_created_at = CAST(:lxd_created_at AS timestamptz), "
                    "lxd_updated_at = CAST(:lxd_updated_at AS timestamptz), "
                    "started_at = COALESCE(task_journal.started_at, "
                    "CAST(:lxd_created_at AS timestamptz)), "
                    "description = COALESCE(NULLIF(task_journal.description, ''), :description), "
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
                "status_code, progress, error, may_cancel, actor_kind, source, metadata, "
                "lxd_created_at, lxd_updated_at, started_at, finished_at) VALUES ("
                "'lxd_operation', :cluster_id, :project, :resource_type, :resource_name, "
                ":operation_id, :operation_class, :action, :description, :new_status, "
                "CAST(:status_code AS integer), :progress, NULLIF(:error, ''), :may_cancel, "
                ":actor_kind, :source, CAST(:metadata AS jsonb), "
                "CAST(:lxd_created_at AS timestamptz), CAST(:lxd_updated_at AS timestamptz), "
                "COALESCE(CAST(:lxd_created_at AS timestamptz), now()), "
                "CASE WHEN :new_status IN ('success', 'failure', 'cancelled') THEN now() END) "
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
                ":resource_name, :operation_id, :action, :description, :new_status, "
                "NULLIF(:audit_error, ''), :actor_email, :actor_role, :actor_kind, :api_key_id, "
                ":source, :request_id, CAST(:target_organization_id AS integer), "
                ":target_organization_slug, CAST(:extra AS jsonb), now(), "
                "CASE WHEN :new_status IN ('success', 'failure') THEN now() END) "
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
    return returned_id(
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
                ":description, :new_status, NULLIF(:audit_error, ''), :actor_email, "
                ":actor_role, :actor_kind, :api_key_id, :source, :request_id, "
                "CAST(:target_organization_id AS integer), :target_organization_slug, "
                "CAST(:extra AS jsonb), "
                "now() - make_interval(secs => COALESCE(CAST(:duration_ms AS float8), 0) / 1000.0), "
                "CASE WHEN :new_status IN ('success', 'failure') THEN now() END) RETURNING id"
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


