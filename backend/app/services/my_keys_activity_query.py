"""What My Keys shows as a key's activity: its dashboard trail and what was done with it on a cluster.

Two sources, one page:

- dashboard rows: the `user.ssh_key.*` audit records in `cluster_log` (create, download, delete,
  each with the client it came from);
- usage rows: what LXD reported under the key's certificate. The follower stores a TLS
  requestor's fingerprint as `actor_email` on an external row, so a row joins the registry
  (`my_key_records.fingerprint`) on that, on the same cluster. Lifecycle events come from
  `cluster_log`; exec and console sessions, which LXD reports as operations only, come from
  `task_journal`.

Scope matches the key list: an administrator reads everything; anyone else reads the keys they
own (or that predate the owner field, on a cluster they reach), plus the dashboard rows of their
own actions. A cleanup run is an administrator's record.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from sqlalchemy import String, and_, cast, func, literal, null, or_, select, union_all

from app.core.task_scope import KEY_RESOURCE_TYPES
from app.models.my_key_models import MyKeyRecord
from app.models.task_journal_models import ClusterLog, TaskJournal

KINDS = ("dashboard", "usage")
OUT_OF_BOUND = "Change outside your project scope"


@dataclass(frozen=True)
class Scope:
    """Who is asking. `allowed_clusters` None means an unrestricted caller (Administrator)."""

    caller_email: str
    allowed_clusters: Optional[Set[str]]

    @property
    def unrestricted(self) -> bool:
        return self.allowed_clusters is None


@dataclass(frozen=True)
class Filters:
    since: datetime
    until: Optional[datetime] = None
    key_id: Optional[str] = None
    action: Optional[str] = None
    result: Optional[str] = None
    kind: Optional[str] = None


def _owned(scope: Scope) -> Any:
    caller = scope.caller_email.lower()
    return or_(
        func.lower(MyKeyRecord.owner_email) == caller,
        and_(MyKeyRecord.owner_email.is_(None), MyKeyRecord.cluster_id.in_(sorted(scope.allowed_clusters or []) or ["\x00"])),
    )


def _dashboard(scope: Scope, filters: Filters) -> Any:
    stmt = (
        select(
            literal("dashboard").label("kind"),
            ClusterLog.ts.label("ts"),
            cast(ClusterLog.id, String).label("row_id"),
            ClusterLog.action.label("action"),
            ClusterLog.result.label("result"),
            ClusterLog.resource_name.label("key_id"),
            MyKeyRecord.name.label("key_name"),
            ClusterLog.cluster_id.label("cluster_id"),
            ClusterLog.project.label("project"),
            ClusterLog.actor_email.label("actor_email"),
            ClusterLog.message.label("message"),
            ClusterLog.details.label("details"),
        )
        .select_from(ClusterLog)
        .outerjoin(MyKeyRecord, MyKeyRecord.key_id == ClusterLog.resource_name)
        .where(ClusterLog.resource_type.in_(KEY_RESOURCE_TYPES), ClusterLog.ts >= filters.since)
    )
    if filters.until is not None:
        stmt = stmt.where(ClusterLog.ts <= filters.until)
    if not scope.unrestricted:
        caller = scope.caller_email.lower()
        stmt = stmt.where(
            or_(
                func.lower(ClusterLog.actor_email) == caller,
                and_(ClusterLog.resource_type == "ssh_key", MyKeyRecord.key_id.is_not(None), _owned(scope)),
            )
        )
    return stmt


def _usage_log(scope: Scope, filters: Filters) -> Any:
    stmt = (
        select(
            literal("usage").label("kind"),
            ClusterLog.ts.label("ts"),
            cast(ClusterLog.id, String).label("row_id"),
            ClusterLog.action.label("action"),
            ClusterLog.result.label("result"),
            MyKeyRecord.key_id.label("key_id"),
            MyKeyRecord.name.label("key_name"),
            ClusterLog.cluster_id.label("cluster_id"),
            ClusterLog.project.label("project"),
            null().label("actor_email"),
            ClusterLog.message.label("message"),
            null().label("details"),
        )
        .select_from(ClusterLog)
        .join(
            MyKeyRecord,
            and_(
                func.lower(ClusterLog.actor_email) == MyKeyRecord.fingerprint,
                ClusterLog.cluster_id == MyKeyRecord.cluster_id,
            ),
        )
        .where(ClusterLog.actor_kind == "external", ClusterLog.ts >= filters.since)
    )
    if filters.until is not None:
        stmt = stmt.where(ClusterLog.ts <= filters.until)
    return _usage_scope(stmt, scope, ClusterLog.cluster_id)


def _usage_tasks(scope: Scope, filters: Filters) -> Any:
    stmt = (
        select(
            literal("usage").label("kind"),
            TaskJournal.created_at.label("ts"),
            cast(TaskJournal.id, String).label("row_id"),
            TaskJournal.action.label("action"),
            TaskJournal.status.label("result"),
            MyKeyRecord.key_id.label("key_id"),
            MyKeyRecord.name.label("key_name"),
            TaskJournal.cluster_id.label("cluster_id"),
            TaskJournal.project.label("project"),
            null().label("actor_email"),
            TaskJournal.description.label("message"),
            null().label("details"),
        )
        .select_from(TaskJournal)
        .join(
            MyKeyRecord,
            and_(
                func.lower(TaskJournal.actor_email) == MyKeyRecord.fingerprint,
                TaskJournal.cluster_id == MyKeyRecord.cluster_id,
            ),
        )
        # note: only the sessions (exec, console), which LXD reports as operations alone. A change
        # is already in cluster_log as its lifecycle event, and listing both would show it twice.
        .where(
            TaskJournal.actor_kind == "external",
            TaskJournal.operation_class == "websocket",
            TaskJournal.created_at >= filters.since,
        )
    )
    if filters.until is not None:
        stmt = stmt.where(TaskJournal.created_at <= filters.until)
    return _usage_scope(stmt, scope, TaskJournal.cluster_id)


def _usage_scope(stmt: Any, scope: Scope, cluster_column: Any) -> Any:
    if scope.unrestricted:
        return stmt
    return stmt.where(_owned(scope), cluster_column.in_(sorted(scope.allowed_clusters or []) or ["\x00"]))


def activity_query(scope: Scope, filters: Filters) -> Any:
    """The union of every row the caller may read, as a subquery, before paging."""
    parts = []
    if filters.kind in (None, "dashboard"):
        parts.append(_dashboard(scope, filters))
    if filters.kind in (None, "usage"):
        parts.extend([_usage_log(scope, filters), _usage_tasks(scope, filters)])
    union = union_all(*parts).subquery("activity")
    conditions = []
    if filters.key_id:
        conditions.append(union.c.key_id == filters.key_id)
    if filters.action:
        conditions.append(union.c.action == filters.action)
    if filters.result:
        conditions.append(union.c.result == filters.result)
    return union, conditions


def present(row: Any, scope: Scope, visible_projects_for: Any) -> Dict[str, Any]:
    """One row for the caller: an out-of-bound project redacted, the client only for its owner."""
    details = row.details if isinstance(row.details, dict) else None
    client = details.get("client") if details else None
    if client is not None and not scope.unrestricted:
        if (row.actor_email or "").lower() != scope.caller_email.lower():
            client = None
    project, message = row.project, row.message
    if row.kind == "usage" and not scope.unrestricted and row.cluster_id:
        visible = visible_projects_for(row.cluster_id)
        if visible is not None and (project or "default") not in visible:
            project, message = None, OUT_OF_BOUND
    return {
        "id": f"{row.kind}:{row.row_id}",
        "ts": row.ts,
        "kind": row.kind,
        "action": row.action,
        "result": row.result,
        "key_id": row.key_id,
        "key_name": row.key_name,
        "cluster_id": row.cluster_id,
        "project": project,
        "actor_email": row.actor_email,
        "message": message or "",
        "client": client,
    }


def neutralise(cell: Any) -> str:
    """A CSV cell a spreadsheet will not run: a leading = + - @ tab or carriage return is quoted."""
    text = "" if cell is None else str(cell)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def last_used_query(scope: Scope) -> Any:
    """Per visible key: its latest usage row in the live tables, beside the registry's roll-up."""
    usage = union_all(
        select(MyKeyRecord.key_id.label("key_id"), ClusterLog.ts.label("ts"), ClusterLog.action.label("action"))
        .select_from(ClusterLog)
        .join(MyKeyRecord, and_(func.lower(ClusterLog.actor_email) == MyKeyRecord.fingerprint,
                                ClusterLog.cluster_id == MyKeyRecord.cluster_id))
        .where(ClusterLog.actor_kind == "external"),
        select(MyKeyRecord.key_id.label("key_id"), TaskJournal.created_at.label("ts"), TaskJournal.action.label("action"))
        .select_from(TaskJournal)
        .join(MyKeyRecord, and_(func.lower(TaskJournal.actor_email) == MyKeyRecord.fingerprint,
                                TaskJournal.cluster_id == MyKeyRecord.cluster_id))
        .where(TaskJournal.actor_kind == "external"),
    ).subquery("used")
    latest = select(usage.c.key_id, func.max(usage.c.ts).label("ts")).group_by(usage.c.key_id).subquery("latest")
    stmt = (
        select(
            MyKeyRecord.key_id,
            MyKeyRecord.cluster_id,
            latest.c.ts.label("live_ts"),
            MyKeyRecord.last_used_at,
            MyKeyRecord.last_used_action,
        )
        .select_from(MyKeyRecord)
        .outerjoin(latest, latest.c.key_id == MyKeyRecord.key_id)
    )
    if not scope.unrestricted:
        stmt = stmt.where(_owned(scope))
    return stmt


def _aware(value: datetime) -> datetime:
    # note: SQLite test sessions hand back naive values for timestamptz columns; they were UTC.
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def latest_of(row: Any) -> Optional[datetime]:
    """The later of the live tables' last use and the roll-up the prune left in the registry."""
    candidates: List[datetime] = [_aware(value) for value in (row.live_ts, row.last_used_at) if value is not None]
    return max(candidates) if candidates else None
