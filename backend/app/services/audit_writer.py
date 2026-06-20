"""Append-only, hash-chained audit writer for the mini deployment.

When the full version's OpenSearch sink is absent, audit events are persisted to the
Postgres ``audit_log`` table. Each row links to its predecessor via
row_hash = sha256(prev_hash + canonical(event)), so in-place edits and reordering are
tamper-evident.

The chain head must be serialized ACROSS PROCESSES (the backend runs multiple uvicorn
workers), so every append takes a Postgres transaction-scoped advisory lock, reads the
true current head from the DB under that lock, computes the next hash, inserts, and
commits (releasing the lock). A per-process asyncio queue decouples the write from the
request path. Set the app DB role to INSERT+SELECT only in production so rows are immutable.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import select, text

logger = logging.getLogger(__name__)

_QUEUE_MAXSIZE = 10000
# Fixed advisory-lock key ("ORCA") that serializes audit_log appends across all workers.
_AUDIT_CHAIN_LOCK = 0x4F524341


def _canonical(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _row_hash(prev_hash: str, payload: Dict[str, Any]) -> str:
    return hashlib.sha256((prev_hash + _canonical(payload)).encode("utf-8")).hexdigest()


# important (M5): every forensic/attribution field is in the hash, so an in-place edit of
# source_ip / actor / request_id / severity / target is tamper-evident, not just the core
# fields. The verifier (routers/audit_log.py) MUST reconstruct exactly this field set.
HASH_FIELDS = (
    "event_id", "occurred_at", "actor_id", "actor_email", "actor_role",
    "action", "category", "severity", "result",
    "target_type", "target_id", "target_host", "target_project",
    "request_id", "source_ip", "details",
)


class AuditWriter:
    """Cross-process-serialized writer for the hash-chained audit_log table."""

    def __init__(self) -> None:
        self._queue: Optional[asyncio.Queue] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._running = True
        self._task = asyncio.create_task(self._run(), name="audit-writer")
        logger.info("Audit DB writer started")

    async def stop(self) -> None:
        self._running = False
        if self._queue is not None:
            try:
                self._queue.put_nowait(None)  # sentinel to wake the worker
            except asyncio.QueueFull:
                pass
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except Exception:
                self._task.cancel()
        logger.info("Audit DB writer stopped")

    def enqueue(self, audit_log: Dict[str, Any]) -> None:
        """Non-blocking enqueue from the (sync) activity logger."""
        if not self._running or self._queue is None:
            return
        try:
            self._queue.put_nowait(audit_log)
        except asyncio.QueueFull:
            # critical: a dropped audit event is a security-visibility loss. Security-critical
            # events should use write_now() (blocking, fail-closed) instead of enqueue().
            logger.critical("Audit queue full - dropping event %s", audit_log.get("event_id"))

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            event = await self._queue.get()
            if event is None:  # shutdown sentinel
                if not self._running:
                    break
                continue
            try:
                await self._persist(event)
            except Exception as e:
                logger.error("Failed to persist audit event: %s", e)

    async def write_now(self, event: Dict[str, Any]) -> None:
        """Durable, awaited append for security-critical events - RAISES on failure
        (fail-closed). The caller must surface the error so a sensitive operation never
        completes silently without an audit record."""
        await self._persist(event)

    async def _persist(self, event: Dict[str, Any]) -> None:
        from app.core.database import async_session_maker
        from app.models.db_models import AuditLogEntry
        payload = self._map(event)
        payload_for_hash = {k: payload[k] for k in HASH_FIELDS}
        async with async_session_maker() as session:
            # Serialize the chain head across ALL workers/processes. The lock is held
            # until commit, so read-head + insert is atomic cluster-wide.
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:k)"), {"k": _AUDIT_CHAIN_LOCK}
            )
            prev = (
                await session.execute(
                    select(AuditLogEntry.row_hash).order_by(AuditLogEntry.id.desc()).limit(1)
                )
            ).scalar_one_or_none() or ""
            rhash = _row_hash(prev, payload_for_hash)
            session.add(AuditLogEntry(prev_hash=prev or None, row_hash=rhash, **payload))
            await session.commit()

    @staticmethod
    def _map(a: Dict[str, Any]) -> Dict[str, Any]:
        actor = a.get("actor") or {}
        target = a.get("target") or {}
        ts = a.get("@timestamp")
        try:
            occurred = datetime.fromisoformat(ts) if ts else datetime.now(timezone.utc)
        except Exception:
            occurred = datetime.now(timezone.utc)
        return {
            "event_id": a.get("event_id") or "",
            "occurred_at": occurred,
            "actor_id": actor.get("user_id"),
            "actor_email": actor.get("email") or actor.get("user_email"),
            "actor_role": actor.get("role"),
            "action": a.get("action") or "",
            "category": a.get("category"),
            "severity": a.get("severity"),
            "result": a.get("result"),
            "target_type": target.get("type"),
            "target_id": target.get("id"),
            "target_host": target.get("host"),
            "target_project": target.get("project"),
            "request_id": a.get("request_id"),
            "source_ip": a.get("source_ip"),
            "details": a.get("details"),
        }


_audit_writer: Optional[AuditWriter] = None


def get_audit_writer() -> AuditWriter:
    global _audit_writer
    if _audit_writer is None:
        _audit_writer = AuditWriter()
    return _audit_writer
