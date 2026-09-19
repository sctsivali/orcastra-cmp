"""The task journal's sweeps and the follower's per-cluster state, as raw SQL.

The write path in `repository` opens and closes rows one event at a time; these run on a timer
and repair what the event stream could not: racing pairs merged, vanished operations marked
lost, stale dashboard actions closed, old rows pruned. Nothing here commits either.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.task_journal.repository_types import (
    FOLLOWER_LOCK,
    LOST_MESSAGE,
    STALE_MESSAGE,
    clean_message,
)


async def merge_racing_pairs(db: AsyncSession, *, window_s: int = 900) -> int:
    # note: the residual race is a dashboard row and an LXD row for one key that neither side
    # claimed because both landed inside the same instant. The LXD row keeps the operation and
    # takes the actor; the dashboard row's log lines move over before it goes.
    pairs = (
        await db.execute(
            text(
                "SELECT d.id AS dash_id, l.id AS lxd_id FROM task_journal d "
                "JOIN task_journal l ON l.cluster_id = d.cluster_id AND l.project = d.project "
                "AND l.resource_type = d.resource_type AND l.resource_name = d.resource_name "
                "AND l.action = d.action "
                "WHERE d.kind = 'dashboard_action' AND d.operation_id IS NULL "
                "AND l.kind = 'lxd_operation' AND l.operation_id IS NOT NULL "
                "AND l.actor_email IS NULL AND l.api_key_id IS NULL "
                "AND d.created_at >= now() - make_interval(secs => CAST(:window_s AS float8)) "
                "AND l.created_at >= now() - make_interval(secs => CAST(:window_s AS float8)) "
                "ORDER BY d.created_at LIMIT 200 FOR UPDATE OF d, l SKIP LOCKED"
            ),
            {"window_s": float(window_s)},
        )
    ).all()
    merged = 0
    used: set = set()
    for pair in pairs:
        dash_id, lxd_id = str(pair[0]), str(pair[1])
        if dash_id in used or lxd_id in used:
            continue
        used.update((dash_id, lxd_id))
        ids = {"dash": dash_id, "lxd": lxd_id}
        await db.execute(
            text(
                "UPDATE task_journal AS l SET actor_email = d.actor_email, "
                "actor_role = d.actor_role, actor_kind = d.actor_kind, "
                "api_key_id = d.api_key_id, source = d.source, "
                "request_id = COALESCE(l.request_id, d.request_id), "
                "target_organization_id = COALESCE(l.target_organization_id, "
                "d.target_organization_id), "
                "target_organization_slug = COALESCE(l.target_organization_slug, "
                "d.target_organization_slug), "
                "error = COALESCE(l.error, d.error), metadata = d.metadata || l.metadata, "
                "updated_at = now(), seq = nextval('task_journal_seq') "
                "FROM task_journal AS d "
                "WHERE l.id = CAST(:lxd AS uuid) AND d.id = CAST(:dash AS uuid)"
            ),
            ids,
        )
        await db.execute(
            text(
                "UPDATE cluster_log SET task_id = CAST(:lxd AS uuid) "
                "WHERE task_id = CAST(:dash AS uuid)"
            ),
            ids,
        )
        await db.execute(
            text("DELETE FROM task_journal WHERE id = CAST(:dash AS uuid)"), ids
        )
        merged += 1
    return merged


async def mark_lost(
    db: AsyncSession, *, cluster_id: str, seen_ids: Iterable[str], older_than_s: int
) -> int:
    result = await db.execute(
        text(
            "UPDATE task_journal SET status = 'lost', "
            "error = COALESCE(task_journal.error, :message), "
            "finished_at = COALESCE(task_journal.finished_at, now()), "
            "updated_at = now(), seq = nextval('task_journal_seq') "
            "WHERE cluster_id = :cluster_id AND kind = 'lxd_operation' "
            "AND status IN ('pending', 'running') "
            "AND updated_at < now() - make_interval(secs => CAST(:older_than_s AS float8)) "
            "AND operation_id <> ALL(CAST(:seen AS text[]))"
        ),
        {
            "cluster_id": cluster_id,
            "message": LOST_MESSAGE,
            "older_than_s": float(older_than_s),
            "seen": [str(s) for s in seen_ids if s],
        },
    )
    return max(result.rowcount, 0)


async def expire_stale_actions(db: AsyncSession, *, older_than_s: int = 1800) -> int:
    result = await db.execute(
        text(
            "UPDATE task_journal SET status = 'lost', "
            "error = COALESCE(task_journal.error, :message), "
            "finished_at = COALESCE(task_journal.finished_at, now()), "
            "updated_at = now(), seq = nextval('task_journal_seq') "
            "WHERE kind IN ('dashboard_action', 'schedule', 'upload') "
            "AND status IN ('pending', 'running') "
            "AND updated_at < now() - make_interval(secs => CAST(:older_than_s AS float8))"
        ),
        {"message": STALE_MESSAGE, "older_than_s": float(older_than_s)},
    )
    return max(result.rowcount, 0)


async def set_cluster_state(
    db: AsyncSession,
    cluster_id: str,
    state: str,
    *,
    error: Optional[str] = None,
    seeded: bool = False,
) -> None:
    await db.execute(
        text(
            "INSERT INTO task_follower_clusters (cluster_id, state, since, last_error, "
            "seeded_at, updated_at) VALUES (:cluster_id, :state, now(), :error, "
            "CASE WHEN :seeded THEN now() END, now()) "
            "ON CONFLICT (cluster_id) DO UPDATE SET state = EXCLUDED.state, "
            "since = CASE WHEN task_follower_clusters.state IS DISTINCT FROM EXCLUDED.state "
            "THEN now() ELSE task_follower_clusters.since END, "
            "last_error = EXCLUDED.last_error, "
            "seeded_at = CASE WHEN :seeded THEN now() ELSE task_follower_clusters.seeded_at END, "
            "updated_at = now()"
        ),
        {"cluster_id": cluster_id, "state": state, "error": clean_message(error), "seeded": bool(seeded)},
    )


async def touch_cluster_frame(db: AsyncSession, cluster_id: str) -> None:
    await db.execute(
        text(
            "UPDATE task_follower_clusters SET last_frame_at = now(), updated_at = now() "
            "WHERE cluster_id = :cluster_id"
        ),
        {"cluster_id": cluster_id},
    )


async def reset_cluster_states(db: AsyncSession) -> None:
    await db.execute(
        text(
            "UPDATE task_follower_clusters SET state = 'disconnected', since = now(), "
            "updated_at = now() WHERE state <> 'disconnected'"
        )
    )


async def read_cluster_states(db: AsyncSession) -> List[Mapping]:
    result = await db.execute(
        text(
            "SELECT cluster_id, state, since, last_frame_at, last_error, seeded_at, updated_at "
            "FROM task_follower_clusters ORDER BY cluster_id"
        )
    )
    return list(result.mappings().all())


async def follower_is_active(db: AsyncSession) -> bool:
    # important: the two-int32 lock form lands in pg_locks as classid/objid with objsubid 2;
    # the 64-bit form would split differently and never match this predicate.
    result = await db.execute(
        text(
            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
            "AND classid = :classid AND objid = :objid AND objsubid = 2 AND granted "
            "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()))"
        ),
        {"classid": FOLLOWER_LOCK[0], "objid": FOLLOWER_LOCK[1]},
    )
    return bool(result.scalar())


async def _delete_batches(db: AsyncSession, statement: str, params: Dict[str, Any], batch: int) -> int:
    total = 0
    while True:
        result = await db.execute(text(statement), {**params, "batch": batch})
        count = max(result.rowcount, 0)
        total += count
        if count < batch:
            return total


async def prune(
    db: AsyncSession, *, task_days: int, log_days: int, batch: int = 5000
) -> Tuple[int, int]:
    await db.execute(
        text(
            "UPDATE task_journal SET status = 'lost', "
            "error = COALESCE(task_journal.error, :message), "
            "finished_at = COALESCE(task_journal.finished_at, now()), "
            "updated_at = now(), seq = nextval('task_journal_seq') "
            "WHERE status IN ('pending', 'running') "
            "AND created_at < now() - make_interval(days => CAST(:days AS integer))"
        ),
        {"message": STALE_MESSAGE, "days": int(task_days)},
    )
    logs = await _delete_batches(
        db,
        "DELETE FROM cluster_log WHERE id IN (SELECT id FROM cluster_log "
        "WHERE ts < now() - make_interval(days => CAST(:days AS integer)) LIMIT :batch)",
        {"days": int(log_days)},
        batch,
    )
    tasks = await _delete_batches(
        db,
        "DELETE FROM task_journal WHERE id IN (SELECT id FROM task_journal "
        "WHERE created_at < now() - make_interval(days => CAST(:days AS integer)) LIMIT :batch)",
        {"days": int(task_days)},
        batch,
    )
    return tasks, logs
