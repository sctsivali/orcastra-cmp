"""Read + integrity-verify the Postgres audit log (mini deployment).

Admin-only. Replaces the OpenSearch audit dashboards when centralized logging is absent.
The verify endpoint recomputes the hash chain to detect in-place edits or reordering.
"""

import csv
import hashlib
import io
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db, async_session_maker
from app.core.dependencies import AuthenticatedUser, require_admin
from app.models.db_models import AuditLogEntry, AuditLogAnchor
from app.services.audit_writer import HASH_FIELDS
from app.services.activity_logger import get_activity_logger

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

# Hard ceiling on a single export so a huge log cannot exhaust memory/connection time.
# Streamed row-by-row, but still bounded; a hit is reported in the audit event + (JSON) body.
EXPORT_MAX_ROWS = 100_000

# Column order for both CSV header and JSON objects (mirrors AuditEntryOut).
_EXPORT_COLUMNS = [
    "id", "event_id", "occurred_at", "actor_id", "actor_email", "actor_role",
    "action", "category", "severity", "result", "target_type", "target_id",
    "target_host", "target_project", "source_ip", "details",
]

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


def _audit_conditions(
    action: Optional[str], actor: Optional[str], result: Optional[str],
    start: Optional[str], end: Optional[str],
) -> list:
    """Build the SQLAlchemy filter list shared by the list and export endpoints."""
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
            conditions.append(AuditLogEntry.occurred_at < end_dt + timedelta(days=1))
        else:
            conditions.append(AuditLogEntry.occurred_at <= end_dt)
    return conditions


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
    conditions = _audit_conditions(action, actor, result, start, end)

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


def _csv_value(row: AuditLogEntry, col: str):
    val = getattr(row, col)
    if val is None:
        return ""
    if col == "occurred_at":
        return val.isoformat()
    if col == "details":
        return json.dumps(val, default=str)
    return val


def _json_value(row: AuditLogEntry, col: str):
    val = getattr(row, col)
    if col == "occurred_at" and val is not None:
        return val.isoformat()
    return val


@router.get("/export")
async def export_audit(
    db: AsyncSession = Depends(get_db),
    _admin: AuthenticatedUser = Depends(require_admin()),
    format: str = Query("csv", description="csv or json"),
    action: Optional[str] = None,
    actor: Optional[str] = None,
    result: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    sort_by: str = Query("occurred_at"),
    sort_dir: str = Query("desc"),
):
    """Stream the audit log as CSV or JSON honoring the active filters (admin-only).

    Exports the FULL filtered set (not a page), capped at EXPORT_MAX_ROWS and streamed
    row-by-row. The export is itself recorded as a fail-closed ``audit.export`` event.
    """
    fmt = format.lower()
    if fmt not in ("csv", "json"):
        raise HTTPException(status_code=400, detail="format must be 'csv' or 'json'")

    conditions = _audit_conditions(action, actor, result, start, end)
    sort_col = _SORTABLE.get(sort_by, AuditLogEntry.occurred_at)
    direction = sort_col.asc() if sort_dir == "asc" else sort_col.desc()

    # Snapshot the upper id bound BEFORE writing the audit.export event below, then bound
    # both the count and the stream to it. This keeps meta.exported == streamed rows and
    # excludes the export's own (higher-id) audit row from its own output.
    max_id_q = select(func.max(AuditLogEntry.id))
    count_q = select(func.count()).select_from(AuditLogEntry)
    base = select(AuditLogEntry)
    for c in conditions:
        max_id_q = max_id_q.where(c)
        count_q = count_q.where(c)
        base = base.where(c)
    snapshot_max = (await db.execute(max_id_q)).scalar() or 0
    count_q = count_q.where(AuditLogEntry.id <= snapshot_max)
    base = base.where(AuditLogEntry.id <= snapshot_max)
    base = base.order_by(direction, AuditLogEntry.id.desc()).limit(EXPORT_MAX_ROWS)

    matched = (await db.execute(count_q)).scalar_one()
    truncated = matched > EXPORT_MAX_ROWS
    exported = min(matched, EXPORT_MAX_ROWS)
    filters = {"action": action, "actor": actor, "result": result, "start": start, "end": end}

    # Exporting the audit log is security-relevant - record it fail-closed BEFORE streaming,
    # so the export is captured even if the client aborts the download mid-stream.
    await activity_logger.log_security_event(
        action="audit.export",
        target_type="audit_log",
        target_id=fmt,
        result="success",
        details={"format": fmt, "filters": filters, "matched": matched,
                 "exported": exported, "truncated": truncated},
    )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    headers = {
        "Content-Disposition": f'attachment; filename="audit-log-{stamp}.{fmt}"',
        "Cache-Control": "no-store",
    }

    if fmt == "csv":
        async def gen_csv():
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(_EXPORT_COLUMNS)
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)
            # Own session so streaming survives the request-scoped session lifecycle.
            async with async_session_maker() as session:
                stream = await session.stream(base)
                async for row in stream.scalars():
                    writer.writerow([_csv_value(row, col) for col in _EXPORT_COLUMNS])
                    yield buf.getvalue()
                    buf.seek(0)
                    buf.truncate(0)
        return StreamingResponse(gen_csv(), media_type="text/csv; charset=utf-8", headers=headers)

    # JSON: include the chain head hash (at the snapshot bound) so an export is tied to the
    # chain state at export time and stays consistent with the bounded item set.
    head_hash = (
        await db.execute(
            select(AuditLogEntry.row_hash)
            .where(AuditLogEntry.id <= snapshot_max)
            .order_by(AuditLogEntry.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    async def gen_json():
        meta = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "filters": filters,
            "matched": matched,
            "exported": exported,
            "truncated": truncated,
            "integrity": {"head_hash": head_hash},
        }
        yield '{"meta":' + json.dumps(meta, default=str) + ',"items":['
        first = True
        async with async_session_maker() as session:
            stream = await session.stream(base)
            async for row in stream.scalars():
                item = {col: _json_value(row, col) for col in _EXPORT_COLUMNS}
                yield ("" if first else ",") + json.dumps(item, default=str)
                first = False
        yield "]}"
    return StreamingResponse(gen_json(), media_type="application/json", headers=headers)


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
