"""A key's activity for My Keys: its dashboard trail and what was done with it on a cluster.

Mounted with the task journal (main.py), because both halves are journal rows: with the journal
off there is nothing to read and the page says so. The rows themselves are a best-effort copy of
the audit; the audit index (OpenSearch) stays the system of record.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthenticatedUser
from app.core.database import get_db
from app.core.dependencies import require_partner
from app.core.org_scope import (
    get_current_allowed_clusters,
    get_current_visible_projects,
    org_scope_dependency,
    visible_projects_for,
)
from app.services import my_keys_activity_query as query
from app.services.activity_logger import get_activity_logger

activity_logger = get_activity_logger()

router = APIRouter(
    prefix="/my-keys",
    tags=["My Keys"],
    dependencies=[Depends(require_partner()), Depends(org_scope_dependency)],
)

DEFAULT_WINDOW = timedelta(days=30)
EXPORT_LIMIT = 10_000


class KeyClient(BaseModel):
    ip: Optional[str] = None
    ip_source: Optional[str] = None
    proxy_trusted: Optional[bool] = None
    user_agent: Optional[str] = None


class KeyActivityEntry(BaseModel):
    id: str
    ts: datetime
    kind: str
    action: Optional[str] = None
    result: Optional[str] = None
    key_id: Optional[str] = None
    key_name: Optional[str] = None
    cluster_id: Optional[str] = None
    project: Optional[str] = None
    actor_email: Optional[str] = None
    message: str = ""
    client: Optional[KeyClient] = None


class KeyActivityPage(BaseModel):
    entries: List[KeyActivityEntry]
    total: int
    limit: int
    offset: int
    has_more: bool


class KeyLastUsed(BaseModel):
    key_id: str
    cluster_id: str
    last_used_at: Optional[datetime] = None


class KeyLastUsedList(BaseModel):
    items: List[KeyLastUsed]


def _scope(user: AuthenticatedUser) -> query.Scope:
    return query.Scope(
        caller_email=user.email or "",
        allowed_clusters=get_current_allowed_clusters(),
        shared_clusters=frozenset(get_current_visible_projects() or {}),
    )


def _filters(
    since: Optional[datetime], until: Optional[datetime], key_id: Optional[str], action: Optional[str],
    result: Optional[str], kind: Optional[str],
) -> query.Filters:
    if kind is not None and kind not in query.KINDS:
        raise HTTPException(status_code=422, detail=f"kind must be one of {', '.join(query.KINDS)}")
    now = datetime.now(timezone.utc)
    start = since if since is not None else now - DEFAULT_WINDOW
    start = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
    end = until if until is None or until.tzinfo else until.replace(tzinfo=timezone.utc)
    return query.Filters(since=start, until=end, key_id=key_id, action=action, result=result, kind=kind)


async def _rows(db: AsyncSession, scope: query.Scope, filters: query.Filters, limit: int, offset: int):
    union, conditions = query.activity_query(scope, filters)
    total = (await db.execute(select(func.count()).select_from(union).where(*conditions))).scalar_one()
    rows = (
        await db.execute(
            select(union).where(*conditions).order_by(union.c.ts.desc(), union.c.row_id.desc()).limit(limit).offset(offset)
        )
    ).all()
    return total, [query.present(row, scope, visible_projects_for) for row in rows]


@router.get("/activity", response_model=KeyActivityPage, summary="What was done with the caller's keys")
async def list_key_activity(
    key_id: Optional[str] = Query(None, max_length=255, description="One key"),
    action: Optional[str] = Query(None, max_length=96, description="One action, for example user.ssh_key.download"),
    result: Optional[str] = Query(None, max_length=16, description="success, failure, denied, or an operation status"),
    kind: Optional[str] = Query(None, description="dashboard or usage"),
    since: Optional[datetime] = Query(None, description="Rows at or after this time; default 30 days ago"),
    until: Optional[datetime] = Query(None, description="Rows at or before this time"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(require_partner()),
) -> KeyActivityPage:
    total, entries = await _rows(db, _scope(user), _filters(since, until, key_id, action, result, kind), limit, offset)
    return KeyActivityPage(entries=entries, total=total, limit=limit, offset=offset, has_more=offset + len(entries) < total)


@router.get("/activity/export.csv", summary="The same rows as a CSV file")
async def export_key_activity(
    key_id: Optional[str] = Query(None, max_length=255),
    action: Optional[str] = Query(None, max_length=96),
    result: Optional[str] = Query(None, max_length=16),
    kind: Optional[str] = Query(None),
    since: Optional[datetime] = Query(None),
    until: Optional[datetime] = Query(None),
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(require_partner()),
) -> StreamingResponse:
    """At most 10,000 rows. Every cell is written so a spreadsheet will not run it as a formula."""
    filters = _filters(since, until, key_id, action, result, kind)
    total, entries = await _rows(db, _scope(user), filters, EXPORT_LIMIT, 0)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["time", "kind", "action", "result", "key", "key_name", "cluster", "project", "actor",
                     "client_ip", "client_ip_source", "user_agent", "message"])
    for entry in entries:
        client = entry.get("client") or {}
        writer.writerow([query.neutralise(v) for v in (
            entry["ts"].isoformat() if entry["ts"] else "", entry["kind"], entry["action"], entry["result"],
            entry["key_id"], entry["key_name"], entry["cluster_id"], entry["project"], entry["actor_email"],
            client.get("ip"), client.get("ip_source"), client.get("user_agent"), entry["message"],
        )])
    activity_logger.log_activity(
        action="user.ssh_key.activity.export",
        target_type="ssh_key",
        target_id=key_id or "all",
        result="success",
        details={"rows": len(entries), "total": total, "truncated": total > len(entries), "kind": kind},
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="key-activity-{stamp}.csv"', "Cache-Control": "no-store"},
    )


@router.get("/last-used", response_model=KeyLastUsedList, summary="When each of the caller's keys last changed something")
async def list_key_last_used(
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(require_partner()),
) -> KeyLastUsedList:
    """The last change LXD reported under each key's certificate. Reads such as `lxc list` leave no event."""
    rows = (await db.execute(query.last_used_query(_scope(user)))).all()
    return KeyLastUsedList(
        items=[KeyLastUsed(key_id=row.key_id, cluster_id=row.cluster_id, last_used_at=query.latest_of(row)) for row in rows]
    )
