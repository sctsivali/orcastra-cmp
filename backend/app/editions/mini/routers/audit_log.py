"""The audit log: browse, verify the chain, export. Administrators only."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthenticatedUser
from app.core.database import get_db
from app.core.dependencies import require_admin
from app.editions.mini import audit
from app.editions.mini.settings import mini_settings
from app.models.mini_identity_models import AuditLogAnchor, AuditLogEntry

router = APIRouter(prefix="/audit-log", tags=["Audit log"])

_SORTABLE = {"id": AuditLogEntry.id, "occurred_at": AuditLogEntry.occurred_at, "action": AuditLogEntry.action,
             "actor_email": AuditLogEntry.actor_email, "result": AuditLogEntry.result}


class AuditRow(BaseModel):
    id: int
    event_id: str
    occurred_at: datetime
    actor_email: Optional[str]
    actor_role: Optional[str]
    action: str
    category: Optional[str]
    severity: Optional[str]
    result: Optional[str]
    target_type: Optional[str]
    target_id: Optional[str]
    target_host: Optional[str]
    target_project: Optional[str]
    source_ip: Optional[str]
    request_id: Optional[str]
    details: Optional[Dict[str, Any]]


class AuditPage(BaseModel):
    items: List[AuditRow]
    total: int
    page: int
    page_size: int


@router.get("", response_model=AuditPage)
async def list_audit(
    _user: AuthenticatedUser = Depends(require_admin()),
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1, le=100_000),
    page_size: int = Query(50, ge=1, le=200),
    action: Optional[str] = Query(None, max_length=120),
    actor: Optional[str] = Query(None, max_length=255),
    result: Optional[str] = Query(None, pattern="^(success|failure|denied)$"),
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    sort: str = Query("occurred_at", pattern="^(id|occurred_at|action|actor_email|result)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
):
    query = select(AuditLogEntry)
    count = select(func.count()).select_from(AuditLogEntry)
    filters = []
    if action:
        filters.append(AuditLogEntry.action.ilike(f"%{action}%"))
    if actor:
        filters.append(func.lower(AuditLogEntry.actor_email).contains(actor.lower()))
    if result:
        filters.append(AuditLogEntry.result == result)
    if start:
        filters.append(AuditLogEntry.occurred_at >= start)
    if end:
        filters.append(AuditLogEntry.occurred_at <= end)
    for condition in filters:
        query, count = query.where(condition), count.where(condition)
    column = _SORTABLE[sort]
    query = query.order_by(column.desc() if order == "desc" else column.asc(), AuditLogEntry.id.desc())
    rows = (await db.execute(query.offset((page - 1) * page_size).limit(page_size))).scalars().all()
    total = (await db.execute(count)).scalar_one()
    return AuditPage(items=[AuditRow.model_validate(r, from_attributes=True) for r in rows], total=total,
                     page=page, page_size=page_size)


@router.get("/verify")
async def verify_chain(user: AuthenticatedUser = Depends(require_admin())):
    report = await audit.verify()
    await audit.security_event("audit.verify", target_type="audit_log", target_id=str(report.get("max_id")),
                               result="success" if report["valid"] else "failure",
                               details={"valid": report["valid"], "checked": report["checked"],
                                        "broken_at_id": report["broken_at_id"]})
    return report


@router.get("/status")
async def audit_status(_user: AuthenticatedUser = Depends(require_admin()), db: AsyncSession = Depends(get_db)):
    anchor = await db.get(AuditLogAnchor, 1)
    writer = audit.get_writer()
    return {
        "retention_days": mini_settings().audit_db_retention_days,
        "rows": (await db.execute(select(func.count()).select_from(AuditLogEntry))).scalar_one(),
        "oldest": (await db.execute(select(func.min(AuditLogEntry.occurred_at)))).scalar_one(),
        "pruned_through_id": anchor.pruned_through_id if anchor else 0,
        "last_verified_max_id": anchor.max_id if anchor else 0,
        "writer": {"running": writer.running, "dropped": writer.dropped, "failed": writer.failed,
                   "last_error": writer.last_error},
    }


@router.get("/export")
async def export_audit(
    user: AuthenticatedUser = Depends(require_admin()),
    format: str = Query("csv", pattern="^(csv|json)$"),
    action: Optional[str] = Query(None, max_length=120),
    actor: Optional[str] = Query(None, max_length=255),
    result: Optional[str] = Query(None, pattern="^(success|failure|denied)$"),
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
):
    """At most 100,000 rows up to the head at the start; the export itself is audited first."""
    filters = {"action": action, "actor": actor, "result": result,
               "start": start.isoformat() if start else None, "end": end.isoformat() if end else None}
    try:
        await audit.security_event("audit.export", target_type="audit_log", target_id=format, severity="high",
                                   details={"format": format, **{k: v for k, v in filters.items() if v}})
    except Exception:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="The export could not be audited, so it did not run")
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    async def _csv():
        yield ",".join(audit.EXPORT_COLUMNS) + "\n"
        async for row in audit.rows(start=start, end=end, action=action, actor=actor, result=result):
            yield audit.csv_line(audit.row_dict(row))

    async def _json():
        yield '{"rows":['
        first = True
        async for row in audit.rows(start=start, end=end, action=action, actor=actor, result=result):
            yield ("" if first else ",") + json.dumps(audit.row_dict(row), default=str)
            first = False
        yield '],"chain_version":%d}' % audit.CHAIN_VERSION

    media = "text/csv" if format == "csv" else "application/json"
    return StreamingResponse(_csv() if format == "csv" else _json(), media_type=media, headers={
        "Content-Disposition": f'attachment; filename="orcastra-mini-audit-{stamp}.{format}"',
        "Cache-Control": "no-store",
    })
