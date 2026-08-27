"""
Session Audit Logger - Persists session lifecycle events to the database.

Records connect/disconnect events with bandwidth and duration data
for compliance, troubleshooting, and usage analytics.

Uses fire-and-forget async writes with retry so audit logging never
blocks the main WebSocket proxy flow but still guarantees delivery.
"""

import logging
import asyncio
from app.core.database import async_session_maker
from app.models.db_models import SessionAuditLog

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_BACKOFF = 0.5  # seconds, doubles each retry


async def _write_audit_with_retry(entry_kwargs: dict) -> None:
    """Write a single audit log entry with exponential-backoff retry."""
    for attempt in range(_MAX_RETRIES):
        try:
            async with async_session_maker() as db:
                entry = SessionAuditLog(**entry_kwargs)
                db.add(entry)
                await db.commit()
                return  # success
        except Exception:
            if attempt < _MAX_RETRIES - 1:
                wait = _RETRY_BACKOFF * (2 ** attempt)
                logger.warning(
                    "Audit log write attempt %d/%d failed, retrying in %.1fs",
                    attempt + 1, _MAX_RETRIES, wait, exc_info=True,
                )
                await asyncio.sleep(wait)
            else:
                logger.error(
                    "Audit log write FAILED after %d attempts — event lost",
                    _MAX_RETRIES, exc_info=True,
                )


async def log_session_connect(
    session_id: str,
    instance_name: str,
    host: str,
    project: str,
    protocol: str,
    user_id: str,
    user_email: str,
    user_role: str,
    target_organization_id: int | None = None,
    target_org_slug: str | None = None,
    break_glass_grant_id: int | None = None,
) -> None:
    """Log a session connect event to the audit table."""
    await _write_audit_with_retry({
        "session_id": session_id,
        "event_type": "connect",
        "instance_name": instance_name,
        "host": host,
        "project": project,
        "protocol": protocol,
        "user_id": user_id,
        "user_email": user_email,
        "user_role": user_role,
        "target_organization_id": target_organization_id,
        "target_org_slug": target_org_slug,
        "break_glass_grant_id": break_glass_grant_id,
    })
    logger.debug("Audit: session %s connected (%s@%s)", session_id, user_email, instance_name)


async def log_session_disconnect(
    session_id: str,
    instance_name: str,
    host: str,
    project: str,
    protocol: str,
    user_id: str,
    user_email: str,
    user_role: str,
    duration_seconds: int,
    download_bytes: int,
    upload_bytes: int,
    target_organization_id: int | None = None,
    target_org_slug: str | None = None,
    break_glass_grant_id: int | None = None,
) -> None:
    """Log a session disconnect event with metrics to the audit table."""
    await _write_audit_with_retry({
        "session_id": session_id,
        "event_type": "disconnect",
        "instance_name": instance_name,
        "host": host,
        "project": project,
        "protocol": protocol,
        "user_id": user_id,
        "user_email": user_email,
        "user_role": user_role,
        "duration_seconds": duration_seconds,
        "download_bytes": download_bytes,
        "upload_bytes": upload_bytes,
        "target_organization_id": target_organization_id,
        "target_org_slug": target_org_slug,
        "break_glass_grant_id": break_glass_grant_id,
    })
    logger.debug(
        "Audit: session %s disconnected (%s, %ds, ↓%d ↑%d bytes)",
        session_id, user_email, duration_seconds, download_bytes, upload_bytes,
    )


# important: asyncio holds only a WEAK reference to a running task. A bare
# create_task() whose result nobody keeps can be garbage collected before it runs,
# so a disconnect row - the one carrying duration and byte totals - could simply
# never be written, silently and unreproducibly. Hold a strong reference until the
# write finishes.
_pending: set = set()


def _schedule(coro, event: str) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("No event loop for audit log — skipping %s event", event)
        coro.close()
        return
    task = loop.create_task(coro)
    _pending.add(task)
    task.add_done_callback(_pending.discard)


def fire_and_forget_connect(**kwargs) -> None:
    """Schedule connect audit log as a background task (non-blocking)."""
    _schedule(log_session_connect(**kwargs), "connect")


def fire_and_forget_disconnect(**kwargs) -> None:
    """Schedule disconnect audit log as a background task (non-blocking)."""
    _schedule(log_session_disconnect(**kwargs), "disconnect")
