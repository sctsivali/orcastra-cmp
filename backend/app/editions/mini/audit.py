"""The hash-chained audit log in Postgres: Mini's audit of record (it has no OpenSearch).

Every record the CMP writes on the ``orcastra.audit`` logger is also appended here by a logging
handler, through a queue so a request never waits on it. Security events (sign-in, bootstrap,
certificate issue, renew, revoke, role change, delete, export, prune) are written synchronously
and fail closed: the operation does not complete without its row, and an identity change commits
in the same transaction as its audit row.

Each row's ``row_hash`` is sha256(previous row_hash + canonical(HASH_FIELDS)), so an edited,
reordered or deleted row breaks verification. Appends from every worker serialize on one Postgres
advisory lock. The canonical form is the one Mini 1.0.0-RC1 wrote (``chain_version`` 1), so rows
written before the upgrade verify with rows written after it.
"""

from __future__ import annotations

import asyncio
import contextvars
import csv
import hashlib
import io
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, List, Optional

from sqlalchemy import delete, func, select, text

from app.core.database import async_session_maker
from app.models.mini_identity_models import AuditLogAnchor, AuditLogEntry

logger = logging.getLogger(__name__)

CHAIN_VERSION = 1
CHAIN_LOCK = 0x4F524341  # one advisory lock key for every append, prune and anchor move
QUEUE_MAX = 10_000
VERIFY_BATCH = 5_000
EXPORT_CAP = 100_000
HASH_FIELDS = (
    "event_id", "occurred_at", "actor_id", "actor_email", "actor_role",
    "action", "category", "severity", "result",
    "target_type", "target_id", "target_host", "target_project",
    "request_id", "source_ip", "details",
)


def canonical(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def row_hash(prev_hash: str, payload: Dict[str, Any]) -> str:
    return hashlib.sha256((prev_hash + canonical(payload)).encode("utf-8")).hexdigest()


def _json_safe(value: Any) -> Any:
    # What JSONB will hand back, so the hash taken now is the hash verification recomputes.
    return None if value is None else json.loads(json.dumps(value, default=str))


def map_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """An ``orcastra.audit`` record as audit_log columns."""
    actor = record.get("actor") or {}
    target = record.get("target") or {}
    stamp = record.get("@timestamp")
    try:
        occurred = datetime.fromisoformat(stamp) if stamp else datetime.now(timezone.utc)
    except (TypeError, ValueError):
        occurred = datetime.now(timezone.utc)
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=timezone.utc)
    details = dict(record.get("details") or {})
    if record.get("error_code"):
        details.setdefault("error_code", record.get("error_code"))
    return {
        "event_id": (record.get("event_id") or "")[:64],
        "occurred_at": occurred,
        "actor_id": _clip(actor.get("user_id"), 255),
        "actor_email": _clip(actor.get("email") or actor.get("user_email") or actor.get("user_id"), 255),
        "actor_role": _clip(actor.get("role"), 50),
        "action": _clip(record.get("action") or "", 120),
        "category": _clip(record.get("category"), 60),
        "severity": _clip(record.get("severity"), 20),
        "result": _clip(record.get("result"), 20),
        "target_type": _clip(target.get("type"), 60),
        "target_id": _clip(target.get("id"), 255),
        "target_host": _clip(target.get("host"), 255),
        "target_project": _clip(target.get("project"), 120),
        "request_id": _clip(record.get("request_id"), 64),
        "source_ip": _clip(record.get("source_ip"), 64),
        "details": _json_safe(details) if details else None,
    }


def _clip(value: Any, limit: int) -> Optional[str]:
    return None if value is None else str(value)[:limit]


async def _append(session, columns: Dict[str, Any]) -> AuditLogEntry:
    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": CHAIN_LOCK})
    prev = (await session.execute(
        select(AuditLogEntry.row_hash).order_by(AuditLogEntry.id.desc()).limit(1)
    )).scalar_one_or_none() or ""
    entry = AuditLogEntry(
        prev_hash=prev or None,
        row_hash=row_hash(prev, {k: columns[k] for k in HASH_FIELDS}),
        chain_version=CHAIN_VERSION,
        **columns,
    )
    session.add(entry)
    await session.flush()
    return entry


class AuditWriter:
    """One per worker: a queue for routine records, and a synchronous path for security events."""

    def __init__(self) -> None:
        self._queue: Optional[asyncio.Queue] = None
        self._task: Optional[asyncio.Task] = None
        self.dropped = 0
        self.failed = 0
        self.last_error: Optional[str] = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._queue = asyncio.Queue(maxsize=QUEUE_MAX)
        self._task = asyncio.create_task(self._run(), name="mini-audit-writer")

    async def stop(self) -> None:
        if self._queue is None or self._task is None:
            return
        await self._queue.put(None)
        try:
            await asyncio.wait_for(self._task, timeout=10.0)
        except asyncio.TimeoutError:
            self._task.cancel()
        self._task = None

    def enqueue(self, record: Dict[str, Any]) -> None:
        if self._queue is None:
            return
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            self.dropped += 1
            logger.critical("Audit queue full: dropped event %s", record.get("event_id"))

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            record = await self._queue.get()
            if record is None:
                return
            try:
                async with async_session_maker() as session:
                    await _append(session, map_record(record))
                    await session.commit()
            except Exception as exc:  # noqa: BLE001 - counted and reported by Host Health
                self.failed += 1
                self.last_error = type(exc).__name__
                logger.error("Audit row not written for %s: %s", record.get("event_id"), type(exc).__name__)

    async def write_now(self, record: Dict[str, Any], session=None) -> None:
        """Append synchronously; raises on failure. With `session`, joins its transaction."""
        if session is not None:
            await _append(session, map_record(record))
            return
        async with async_session_maker() as own:
            await _append(own, map_record(record))
            await own.commit()


_writer = AuditWriter()


def get_writer() -> AuditWriter:
    return _writer


_capture: contextvars.ContextVar[Optional[List[Dict[str, Any]]]] = contextvars.ContextVar("mini_audit_capture", default=None)


# Reads the backend makes on its own (the cluster registry and cluster credentials, every few
# seconds) would be about 20,000 rows a day per cluster and bury what operators did. A successful
# read with no actor is left out; a read an operator caused, any failure, and every write stay.
ROUTINE_SYSTEM_READS = frozenset({"vault.kv.list", "vault.secret.read", "vault.cert.list", "vault.health.check"})


def is_routine_system_read(data: Dict[str, Any]) -> bool:
    actor = data.get("actor") or {}
    return (data.get("action") in ROUTINE_SYSTEM_READS and data.get("result", "success") == "success"
            and not (actor.get("user_id") or actor.get("email")))


class AuditSink(logging.Handler):
    """Copies every ``orcastra.audit`` record into the chain, except routine system reads.
    Never raises, never blocks."""

    def emit(self, record: logging.LogRecord) -> None:
        data = getattr(record, "audit_log_data", None)
        if not isinstance(data, dict) or is_routine_system_read(data):
            return
        try:
            data = _with_request_context(data)
            captured = _capture.get()
            if captured is not None:
                captured.append(data)  # a security event writes its own row synchronously
                return
            get_writer().enqueue(data)
        except Exception:  # noqa: BLE001 - logging must never take a request down
            pass


def _with_request_context(data: Dict[str, Any]) -> Dict[str, Any]:
    from app.core.request_context import get_request_context

    ctx = get_request_context()
    if ctx is None:
        return data
    enriched = dict(data)
    if not enriched.get("source_ip") and ctx.client_ip:
        enriched["source_ip"] = ctx.client_ip
    return enriched


async def security_event(action: str, *, target_type: str, target_id: str, result: str = "success",
                         details: Optional[Dict[str, Any]] = None, actor_user_id: Optional[str] = None,
                         severity: Optional[str] = None, session=None) -> str:
    """Write a security event to stdout and the chain; raises if the chain row cannot be written.

    The record goes through the CMP activity logger (same shape, same stdout line), captured here
    so the chain row is written synchronously instead of queued.
    """
    from app.services.activity_logger import get_activity_logger

    token = _capture.set([])
    try:
        event_id = get_activity_logger().log_activity(
            action=action, target_type=target_type, target_id=target_id, result=result,
            details=details, actor_user_id=actor_user_id, severity=severity,
        )
        captured = _capture.get() or []
    finally:
        _capture.reset(token)
    if not captured:
        raise RuntimeError("the audit record was not produced")
    await get_writer().write_now(captured[-1], session=session)
    return event_id


# ---------------------------------------------------------------------------
# Verification, export, retention
# ---------------------------------------------------------------------------


def walk_chain(rows, prev: str):
    """(head hash, last id, rows checked, id of the first broken row or None) over rows in id order."""
    last_id, checked = None, 0
    for row in rows:
        expected = row_hash(prev, {k: getattr(row, k) for k in HASH_FIELDS})
        if (row.prev_hash or "") != prev or row.row_hash != expected:
            return prev, last_id, checked, row.id
        prev, last_id = row.row_hash, row.id
        checked += 1
    return prev, last_id, checked, None


async def verify(advance_anchor: bool = True) -> Dict[str, Any]:
    """Recompute the whole chain in batches; detect edits, reorders, gaps and a deleted tail."""
    async with async_session_maker() as db:
        anchor = await db.get(AuditLogAnchor, 1)
        start_id = anchor.pruned_through_id if anchor else 0
        prev = (anchor.pruned_head_hash or "") if anchor and start_id else ""
        last_id, checked, broken_at = start_id, 0, None
        while broken_at is None:
            rows = (await db.execute(
                select(AuditLogEntry).where(AuditLogEntry.id > last_id).order_by(AuditLogEntry.id).limit(VERIFY_BATCH)
            )).scalars().all()
            if not rows:
                break
            prev, walked_to, walked, broken_at = walk_chain(rows, prev)
            checked += walked
            last_id = walked_to if walked_to is not None else last_id
            db.expunge_all()
        count = (await db.execute(
            select(func.count()).select_from(AuditLogEntry).where(AuditLogEntry.id > start_id)
        )).scalar_one()
        max_id = (await db.execute(select(func.max(AuditLogEntry.id)))).scalar_one() or start_id
        result: Dict[str, Any] = {
            "valid": broken_at is None, "checked": checked, "broken_at_id": broken_at,
            "count": count, "max_id": max_id, "head_hash": prev or None,
            "pruned_through_id": start_id, "note": None,
        }
        if broken_at is not None:
            result["note"] = f"chain broken at id {broken_at}: a row was edited, reordered or removed"
        elif anchor is not None and (max_id < anchor.max_id or (anchor.head_hash and count < anchor.row_count)):
            result["valid"] = False
            result["note"] = (
                f"rows were deleted from the end: {count} rows up to id {max_id}, "
                f"the last verified state was {anchor.row_count} rows up to id {anchor.max_id}"
            )
        if result["valid"] and advance_anchor:
            await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": CHAIN_LOCK})
            anchor = await db.get(AuditLogAnchor, 1)
            if anchor is None:
                anchor = AuditLogAnchor(id=1, max_id=0, row_count=0, pruned_through_id=0)
                db.add(anchor)
            anchor.max_id, anchor.row_count, anchor.head_hash = max_id, count, prev or None
            await db.commit()
        return result


async def prune(retention_days: int) -> Dict[str, Any]:
    """Delete rows older than the retention and record the cut, keeping what remains verifiable."""
    if retention_days <= 0:
        return {"pruned": 0}
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    async with async_session_maker() as db:
        await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": CHAIN_LOCK})
        last = (await db.execute(
            select(AuditLogEntry.id, AuditLogEntry.row_hash).where(AuditLogEntry.occurred_at < cutoff)
            .order_by(AuditLogEntry.id.desc()).limit(1)
        )).first()
        if last is None:
            return {"pruned": 0}
        through_id, through_hash = last
        deleted = (await db.execute(delete(AuditLogEntry).where(AuditLogEntry.id <= through_id))).rowcount or 0
        anchor = await db.get(AuditLogAnchor, 1)
        if anchor is None:
            anchor = AuditLogAnchor(id=1, max_id=0, row_count=0)
            db.add(anchor)
        anchor.pruned_through_id, anchor.pruned_head_hash = through_id, through_hash
        anchor.row_count = max(0, (anchor.row_count or 0) - deleted)
        await db.flush()
        await security_event(
            "audit.prune", target_type="audit_log", target_id=str(through_id), severity="high",
            details={"deleted_rows": deleted, "older_than_days": retention_days, "through_id": through_id},
            session=db,
        )
        await db.commit()
    return {"pruned": deleted, "through_id": through_id}


async def rows(*, start: Optional[datetime], end: Optional[datetime], action: Optional[str],
               actor: Optional[str], result: Optional[str], cap: int = EXPORT_CAP) -> AsyncIterator[AuditLogEntry]:
    """Rows in id order up to the id that was the head when the export began, at most `cap`."""
    async with async_session_maker() as db:
        head = (await db.execute(select(func.max(AuditLogEntry.id)))).scalar_one() or 0
        last_id, sent = 0, 0
        while sent < cap:
            query = select(AuditLogEntry).where(AuditLogEntry.id > last_id, AuditLogEntry.id <= head)
            if start:
                query = query.where(AuditLogEntry.occurred_at >= start)
            if end:
                query = query.where(AuditLogEntry.occurred_at <= end)
            if action:
                query = query.where(AuditLogEntry.action == action)
            if actor:
                query = query.where(func.lower(AuditLogEntry.actor_email).contains(actor.lower()))
            if result:
                query = query.where(AuditLogEntry.result == result)
            batch = (await db.execute(query.order_by(AuditLogEntry.id).limit(min(VERIFY_BATCH, cap - sent)))).scalars().all()
            if not batch:
                return
            for row in batch:
                yield row
            sent += len(batch)
            last_id = batch[-1].id
            db.expunge_all()


EXPORT_COLUMNS = ("id",) + HASH_FIELDS + ("prev_hash", "row_hash", "chain_version")


def row_dict(row: AuditLogEntry) -> Dict[str, Any]:
    out = {name: getattr(row, name) for name in EXPORT_COLUMNS}
    out["occurred_at"] = row.occurred_at.isoformat() if row.occurred_at else None
    return out


_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _spreadsheet_safe(value: Any) -> Any:
    # A cell a spreadsheet would run as a formula (an operator-chosen name such as
    # "=HYPERLINK(...)") is written as text. The JSON export keeps the exact values the chain hashes.
    if isinstance(value, str) and value.startswith(_FORMULA_START):
        return "'" + value
    return value


def csv_line(values: Dict[str, Any]) -> str:
    buffer = io.StringIO()
    csv.writer(buffer).writerow([
        _spreadsheet_safe(json.dumps(values[c], sort_keys=True) if isinstance(values.get(c), (dict, list)) else values.get(c))
        for c in EXPORT_COLUMNS
    ])
    return buffer.getvalue()
