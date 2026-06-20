"""Read + integrity-verify the Postgres audit log (mini deployment).

Admin-only. Replaces the OpenSearch audit dashboards when centralized logging is absent.
The verify endpoint recomputes the hash chain to detect in-place edits or reordering.
"""

import hashlib
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import AuthenticatedUser, require_admin
from app.models.db_models import AuditLogEntry, AuditLogAnchor
from app.services.audit_writer import HASH_FIELDS

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/audit-log",
    tags=["Audit Log"],
    dependencies=[Depends(require_admin())],
)


class AuditEntryOut(BaseModel):
    id: int
    event_id: str
    occurred_at: datetime
    actor_id: Optional[str] = None
    actor_email: Optional[str] = None
    actor_role: Optional[str] = None
    action: str
    category: Optional[str] = None
    severity: Optional[str] = None
    result: Optional[str] = None
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    target_host: Optional[str] = None
    target_project: Optional[str] = None
    source_ip: Optional[str] = None
    details: Optional[dict] = None


class AuditListResponse(BaseModel):
    total: int
    items: list[AuditEntryOut]


class AuditVerifyResponse(BaseModel):
    valid: bool
    checked: int
    broken_at_id: Optional[int] = None
    count: int = 0
    max_id: int = 0
    head_hash: Optional[str] = None
    note: Optional[str] = None


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


# Columns that may be sorted, mapped to their model attribute (whitelist - never sort by
# a raw user string, to avoid injection / sorting on un-indexed columns).
_SORTABLE = {
    "occurred_at": AuditLogEntry.occurred_at,
    "action": AuditLogEntry.action,
    "actor_id": AuditLogEntry.actor_id,
    "result": AuditLogEntry.result,
    "target_type": AuditLogEntry.target_type,
    "source_ip": AuditLogEntry.source_ip,
}


def _parse_range(value: Optional[str]) -> Optional[datetime]:
    """Parse a date (YYYY-MM-DD) or full ISO datetime; return None if blank/invalid."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@router.get("", response_model=AuditListResponse)
async def list_audit(
    db: AsyncSession = Depends(get_db),
    _admin: AuthenticatedUser = Depends(require_admin()),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    action: Optional[str] = None,
    actor: Optional[str] = None,
    result: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    sort_by: str = Query("occurred_at"),
    sort_dir: str = Query("desc"),
) -> AuditListResponse:
    conditions = []
    if action:
        conditions.append(AuditLogEntry.action == action)
    if actor:
        conditions.append(AuditLogEntry.actor_id == actor)
    if result:
        conditions.append(AuditLogEntry.result == result)

    start_dt = _parse_range(start)
    if start_dt:
        conditions.append(AuditLogEntry.occurred_at >= start_dt)
    end_dt = _parse_range(end)
    if end_dt:
        # A bare date end (no time) is treated as inclusive of that whole day.
        if end and len(end) <= 10:
            end_dt = end_dt + timedelta(days=1)
            conditions.append(AuditLogEntry.occurred_at < end_dt)
        else:
            conditions.append(AuditLogEntry.occurred_at <= end_dt)

    sort_col = _SORTABLE.get(sort_by, AuditLogEntry.occurred_at)
    direction = sort_col.asc() if sort_dir == "asc" else sort_col.desc()

    base = select(AuditLogEntry)
    count_q = select(func.count()).select_from(AuditLogEntry)
    for c in conditions:
        base = base.where(c)
        count_q = count_q.where(c)

    total = (await db.execute(count_q)).scalar_one()
    # id is the tiebreak so equal sort keys keep a stable, chain-order paging.
    rows = (
        await db.execute(
            base.order_by(direction, AuditLogEntry.id.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()

    return AuditListResponse(
        total=total,
        items=[
            AuditEntryOut(
                id=r.id, event_id=r.event_id, occurred_at=r.occurred_at,
                actor_id=r.actor_id, actor_email=r.actor_email, actor_role=r.actor_role,
                action=r.action, category=r.category, severity=r.severity, result=r.result,
                target_type=r.target_type, target_id=r.target_id,
                target_host=r.target_host, target_project=r.target_project,
                source_ip=r.source_ip, details=r.details,
            )
            for r in rows
        ],
    )


@router.get("/verify", response_model=AuditVerifyResponse)
async def verify_chain(
    db: AsyncSession = Depends(get_db),
    _admin: AuthenticatedUser = Depends(require_admin()),
) -> AuditVerifyResponse:
    """Recompute the hash chain in order, detect in-place edits/reordering AND tail
    truncation (via the persisted high-water anchor)."""
    rows = (await db.execute(select(AuditLogEntry).order_by(AuditLogEntry.id.asc()))).scalars().all()
    prev = ""
    checked = 0
    broken_at: Optional[int] = None
    for r in rows:
        # Reconstruct exactly the writer's hashed field set (shared HASH_FIELDS) so any
        # in-place edit of a forensic field (source_ip, actor, request_id, ...) is caught.
        payload = {k: getattr(r, k) for k in HASH_FIELDS}
        expected = hashlib.sha256((prev + _canonical(payload)).encode("utf-8")).hexdigest()
        if expected != r.row_hash:
            broken_at = r.id
            break
        prev = r.row_hash
        checked += 1

    head_hash = prev or None
    count = len(rows)
    max_id = rows[-1].id if rows else 0

    # M4: tail truncation / rollback detection. The chain alone cannot detect deletion of
    # a contiguous suffix (a shorter chain still verifies), so compare against the last
    # anchored high-water mark. A decrease in row count or max id means rows were deleted.
    anchor = (
        await db.execute(select(AuditLogAnchor).where(AuditLogAnchor.id == 1))
    ).scalar_one_or_none()
    note: Optional[str] = None
    truncated = False
    if anchor is not None and (count < anchor.row_count or max_id < anchor.max_id):
        truncated = True
        note = (
            f"tail truncation/rollback detected: rows={count} (anchor {anchor.row_count}), "
            f"max_id={max_id} (anchor {anchor.max_id}) - audit rows were deleted"
        )

    valid = broken_at is None and not truncated

    # Advance the anchor only when the chain is intact and not truncated, so a tamper
    # cannot silently reset the high-water mark.
    if valid:
        if anchor is None:
            db.add(AuditLogAnchor(id=1, max_id=max_id, row_count=count, head_hash=head_hash or ""))
        else:
            anchor.max_id = max_id
            anchor.row_count = count
            anchor.head_hash = head_hash or ""
        await db.commit()

    return AuditVerifyResponse(
        valid=valid, checked=checked, broken_at_id=broken_at,
        count=count, max_id=max_id, head_hash=head_hash, note=note,
    )
