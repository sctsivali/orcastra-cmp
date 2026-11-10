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
    link_lifecycle_rows,
    mark_lost,
    mark_stale_open_rows_lost,
    mark_unregistered_except,
    prune_batch,
    roll_up_key_usage,
    merge_racing_pairs,
    read_cluster_states,
    reset_cluster_states,
    set_cluster_state,
    touch_cluster_frame,
)
from app.services.task_journal.repository_types import (  # noqa: F401 - public re-exports
    FOLLOWER_LOCK,
    fit,
    PRUNE_LOCK,
    ActorFields,
    CorrelationKey,
    OperationFrame,
    UpsertResult,
    clean_message,
    json_param,
    returned_id,
)
from app.services.task_journal.repository_sql import (  # noqa: F401 - shared with tests
    MONOTONIC_STATUS,
    STREAM_STATUS,
    _ACTOR_SET,
    _AUDIT_VERDICT_YIELDS,
    _CLUSTER_VERDICT_FINAL,
    _KEY_BACKFILL,
    _KEY_MATCH,
    _NEW,
    _ON_CONFLICT,
    _OPERATION_GUARD,
    _OPERATION_SET,
    _RESULT_STATUS,
    _STREAM_FINISH,
    _TERMINAL_3,
    _TERMINAL_FINISH,
    _actor_params,
    _effective,
    _frame_params,
    _key_params,
    _require_operation_id,
    _status_for,
)


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

    if frame.action and not (frame.resource_type and frame.resource_name):
        # LXD named no resource ("Creating instance", "Deleting project"): the newest dashboard
        # row for the same action in the same project is this operation, and the row keeps the
        # name the audit gave it. Otherwise the frame would open a nameless second row that no
        # merge could pair.
        attached = returned_id(
            await db.execute(
                text(
                    "UPDATE task_journal SET operation_id = :operation_id, "
                    "kind = CASE WHEN task_journal.kind = 'dashboard_action' "
                    "THEN 'lxd_operation' ELSE task_journal.kind END, "
                    "operation_class = :operation_class, "
                    f"status = {STREAM_STATUS}, "
                    "status_code = CAST(:status_code AS integer), progress = :progress, "
                    "error = COALESCE(NULLIF(:error, ''), task_journal.error), "
                    f"finished_at = {_STREAM_FINISH}, "
                    "may_cancel = :may_cancel, "
                    "lxd_created_at = CAST(:lxd_created_at AS timestamptz), "
                    "lxd_updated_at = CAST(:lxd_updated_at AS timestamptz), "
                    "started_at = COALESCE(task_journal.started_at, "
                    "CAST(:lxd_created_at AS timestamptz)), "
                    "metadata = task_journal.metadata || CAST(:metadata AS jsonb), "
                    "seq = nextval('task_journal_seq'), updated_at = now() "
                    "WHERE id = (SELECT id FROM task_journal WHERE "
                    "cluster_id = :cluster_id AND project = :project AND action = :action "
                    "AND operation_id IS NULL "
                    "AND created_at >= now() - interval '15 minutes' "
                    "AND (status IN ('pending', 'running') OR created_at >= now() - interval '2 minutes') "
                    "ORDER BY (status IN ('pending', 'running')) DESC, created_at DESC "
                    "LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING id"
                ),
                params,
            )
        )
        if attached is not None:
            return UpsertResult(id=attached, created=False, changed=True)

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
                    "error = COALESCE(NULLIF(:error, ''), task_journal.error), "
                    f"finished_at = {_STREAM_FINISH}, "
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
                    # An open row is this operation; a finished one only when the wait=True
                    # router closed it moments ago, never an earlier attempt that failed.
                    "AND (status IN ('pending', 'running') OR created_at >= now() - interval '2 minutes') "
                    "ORDER BY (status IN ('pending', 'running')) DESC, created_at DESC "
                    "LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING id"
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
        "description": fit("description", description or ""),
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
    kind: str = "dashboard_action",
) -> Optional[str]:
    params = {
        **_actor_params(actor),
        **_key_params(key),
        "kind": kind,
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
                "cluster_id = :cluster_id AND project = :project AND action = :action "
                "AND resource_name IS NULL "
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
        "description": fit("description", description or ""),
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


async def find_task_by_key(
    db: AsyncSession, *, key: CorrelationKey, window_s: int = 120
) -> Optional[str]:
    """The newest task for a lifecycle frame's key, claimed or not, for the log link only.

    The dashboard's own action can claim the row before its lifecycle frame arrives (a stop
    before `instance-stopped`); `attach_requestor` then leaves the actor alone, and this lookup
    still links the lifecycle line to the task's Log tab.
    """
    return returned_id(
        await db.execute(
            text(
                "SELECT id FROM task_journal WHERE "
                f"{_KEY_MATCH} AND kind = 'lxd_operation' "
                "AND created_at >= now() - make_interval(secs => CAST(:window_s AS float8)) "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {**_key_params(key), "window_s": float(window_s)},
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


