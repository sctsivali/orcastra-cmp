"""The task journal's sweeps and the follower's per-cluster state, as raw SQL.

The write path in `repository` opens and closes rows one event at a time; these run on a timer
and repair what the event stream could not: racing pairs merged, vanished operations marked
lost, stale dashboard actions closed, old rows pruned. Nothing here commits either.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.task_journal.vocabulary import LIFECYCLE_TO_ACTION
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
                # The same rule as the by-key attach: a finished dashboard row is this operation
                # only when the two landed within two minutes; an earlier failed attempt is not.
                "AND (d.status IN ('pending', 'running') "
                "OR abs(extract(epoch FROM (l.created_at - d.created_at))) <= 120) "
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


async def link_lifecycle_rows(db: AsyncSession, *, window_s: int = 900) -> int:
    """Attach every unlinked lifecycle line of the last `window_s` to the task its key names.

    The writer links a line inline when the task row already exists; this is the backstop for
    the orders it cannot see (the lifecycle frame ahead of the operation frame in one batch,
    a hand-over, a row the sink was still claiming). The closest task in time wins when two
    share a key inside the window. The action map is the vocabulary's, bound as a VALUES list
    so the mapping lives in one place.
    """
    pairs = list(LIFECYCLE_TO_ACTION.items())
    if not pairs:
        return 0
    values = ", ".join(
        f"(CAST(:la{i} AS varchar), CAST(:ta{i} AS varchar))" for i in range(len(pairs))
    )
    params: Dict[str, Any] = {"window_s": float(window_s)}
    for i, (lifecycle_action, task_action) in enumerate(pairs):
        params[f"la{i}"] = lifecycle_action
        params[f"ta{i}"] = task_action
    result = await db.execute(
        text(
            "UPDATE cluster_log AS l SET task_id = c.task_id FROM ("
            "SELECT l2.id AS log_id, ("
            "SELECT t.id FROM task_journal AS t WHERE t.kind = 'lxd_operation' "
            "AND t.cluster_id = l2.cluster_id AND t.project = l2.project "
            "AND t.resource_type = l2.resource_type AND t.resource_name = l2.resource_name "
            "AND t.action = m.task_action "
            "AND t.created_at BETWEEN l2.ts - interval '15 minutes' AND l2.ts + interval '2 minutes' "
            "ORDER BY abs(extract(epoch FROM (l2.ts - t.created_at))) LIMIT 1) AS task_id "
            "FROM cluster_log AS l2 "
            f"JOIN (VALUES {values}) AS m(lifecycle_action, task_action) "
            "ON m.lifecycle_action = l2.action "
            "WHERE l2.kind = 'lifecycle' AND l2.task_id IS NULL AND l2.resource_name IS NOT NULL "
            "AND l2.ts >= now() - make_interval(secs => CAST(:window_s AS float8))"
            ") AS c WHERE c.log_id = l.id AND c.task_id IS NOT NULL "
            "RETURNING l.task_id"
        ),
        params,
    )
    linked = [str(row[0]) for row in result.all() if row[0]]
    for task_id in sorted(set(linked)):
        await db.execute(
            text(
                "UPDATE task_journal SET seq = nextval('task_journal_seq'), updated_at = now() "
                "WHERE id = CAST(:task_id AS uuid)"
            ),
            {"task_id": task_id},
        )
    return len(linked)


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



async def mark_stale_open_rows_lost(db: AsyncSession, *, task_days: int) -> int:
    result = await db.execute(
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
    return max(getattr(result, "rowcount", 0) or 0, 0)


async def prune_batch(
    db: AsyncSession, *, task_days: int, log_days: int, batch: int = 5000, key_days: Optional[int] = None
) -> Tuple[int, int, bool]:
    """One batch of each delete; the third value says whether a full batch was deleted, so the
    caller commits and comes back for more. Nothing here holds more than one batch's rows.

    A My Keys row (resource_type ssh_key, ssh_key_cleanup or ssh_key_request) is a credential's trail and is kept
    for `key_days` (MY_KEYS_AUDIT_RETENTION_DAYS), never less than every other row's `log_days`.
    """
    key_days = max(int(key_days if key_days is not None else log_days), int(log_days))
    logs = await db.execute(
        text(
            "DELETE FROM cluster_log WHERE id IN (SELECT id FROM cluster_log "
            "WHERE ts < now() - make_interval(days => CAST(:days AS integer)) "
            "AND (resource_type IS NULL OR resource_type NOT IN ('ssh_key', 'ssh_key_cleanup', 'ssh_key_request') "
            "OR ts < now() - make_interval(days => CAST(:key_days AS integer))) LIMIT :batch)"
        ),
        {"days": int(log_days), "key_days": key_days, "batch": batch},
    )
    tasks = await db.execute(
        text(
            "DELETE FROM task_journal WHERE id IN (SELECT id FROM task_journal "
            "WHERE created_at < now() - make_interval(days => CAST(:days AS integer)) LIMIT :batch)"
        ),
        {"days": int(task_days), "batch": batch},
    )
    log_count = max(getattr(logs, "rowcount", 0) or 0, 0)
    task_count = max(getattr(tasks, "rowcount", 0) or 0, 0)
    return task_count, log_count, (log_count >= batch or task_count >= batch)


async def roll_up_key_usage(db: AsyncSession, *, log_days: int, task_days: int) -> int:
    """Carry the last change made with each My Key into its registry row before the prune takes it.

    LXD names a TLS identity's requestor by its certificate fingerprint, which the follower stores
    as `actor_email` on an external row. Only rows the prune is about to delete are read: newer
    ones are still in the tables, where the key activity read finds them.
    """
    result = await db.execute(
        text(
            "UPDATE my_key_records AS r SET last_used_at = u.ts, last_used_action = u.action, "
            "last_used_cluster = u.cluster_id "
            "FROM (SELECT DISTINCT ON (fp) fp, ts, action, cluster_id FROM ("
            "  SELECT lower(actor_email) AS fp, ts, action, cluster_id FROM cluster_log "
            "  WHERE actor_kind = 'external' AND actor_email IS NOT NULL "
            "  AND ts < now() - make_interval(days => CAST(:log_days AS integer)) "
            "  UNION ALL "
            "  SELECT lower(actor_email) AS fp, created_at AS ts, action, cluster_id FROM task_journal "
            "  WHERE actor_kind = 'external' AND actor_email IS NOT NULL "
            "  AND created_at < now() - make_interval(days => CAST(:task_days AS integer))"
            ") AS used ORDER BY fp, ts DESC) AS u "
            "WHERE u.fp = r.fingerprint AND (r.last_used_at IS NULL OR u.ts > r.last_used_at)"
        ),
        {"log_days": int(log_days), "task_days": int(task_days)},
    )
    return max(getattr(result, "rowcount", 0) or 0, 0)
