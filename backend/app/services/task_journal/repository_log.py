"""Cluster log rows: one insert idempotent on `event_id`, and the link to a task."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Mapping

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_sanitizer import sanitize_error
from app.services.task_journal.repository_types import fit_params, json_param
from app.services.task_journal.vocabulary import parse_lxd_time

_LOG_COLUMNS = (
    "ts", "kind", "severity", "cluster_id", "project", "resource_type", "resource_name",
    "action", "message", "actor_email", "actor_role", "actor_kind", "api_key_id", "result",
    "request_id", "event_id", "task_id", "target_organization_id", "details",
)


def _log_params(row: Mapping[str, Any]) -> Dict[str, Any]:
    params: Dict[str, Any] = {column: row.get(column) for column in _LOG_COLUMNS}
    # important: asyncpg binds a timestamptz parameter from a datetime only; the audit record
    # and an LXD warning both carry the time as ISO text, and a text that does not parse
    # falls back to now() in the statement rather than dropping the row.
    ts = row.get("ts")
    params["ts"] = ts if isinstance(ts, datetime) else parse_lxd_time(ts)
    params["message"] = sanitize_error(str(row.get("message") or ""), limit=512)
    params["actor_kind"] = row.get("actor_kind") or "system"
    params["details"] = json_param(row["details"]) if row.get("details") is not None else None
    params["task_id"] = str(row["task_id"]) if row.get("task_id") else None
    return fit_params(params)


async def insert_log_rows(db: AsyncSession, rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    result = await db.execute(
        text(
            "INSERT INTO cluster_log (ts, kind, severity, cluster_id, project, resource_type, "
            "resource_name, action, message, actor_email, actor_role, actor_kind, api_key_id, "
            "result, request_id, event_id, task_id, target_organization_id, details) VALUES ("
            "COALESCE(CAST(:ts AS timestamptz), now()), :kind, :severity, :cluster_id, "
            ":project, :resource_type, :resource_name, :action, :message, :actor_email, "
            ":actor_role, :actor_kind, :api_key_id, :result, :request_id, :event_id, "
            "CAST(:task_id AS uuid), CAST(:target_organization_id AS integer), "
            "CAST(:details AS jsonb)) "
            "ON CONFLICT (event_id) WHERE event_id IS NOT NULL DO NOTHING"
        ),
        [_log_params(row) for row in rows],
    )
    count = getattr(result, "rowcount", -1)
    return count if isinstance(count, int) and count >= 0 else len(rows)


async def link_log_to_task(db: AsyncSession, *, event_id: str, task_id: str) -> None:
    await db.execute(
        text(
            "UPDATE cluster_log SET task_id = CAST(:task_id AS uuid) "
            "WHERE event_id = :event_id AND task_id IS NULL"
        ),
        {"event_id": event_id, "task_id": str(task_id)},
    )
