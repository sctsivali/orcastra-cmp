"""
Metrics History Service - Persistent metrics storage backed by PostgreSQL.

Provides non-blocking writes (fire-and-forget with retry) so the background
collector is never slowed by database I/O, plus efficient reads with
in-memory caching for the chart endpoints.

Retention is configurable (default 7 days) and enforced by a periodic
cleanup task that runs alongside the collector.
"""

import logging
import asyncio
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any

from sqlalchemy import select, delete, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session_maker
from app.models.db_models import MetricsSnapshot

logger = logging.getLogger(__name__)

# retention: keep 7 days of 30-second snapshots (~20k rows)
DEFAULT_RETENTION_DAYS = 7

# write retry config (mirrors session_audit.py pattern)
_MAX_RETRIES = 3
_RETRY_BACKOFF = 0.5


# ----------------------------------------------
#  WRITE (fire-and-forget, used by collector)
# ----------------------------------------------

async def persist_metrics_snapshot(
    point: Dict[str, Any],
    scope: str = "all",
) -> None:
    """
    Persist a single metrics data point to PostgreSQL.

    Uses fire-and-forget pattern with retry so it never blocks
    the collector loop.  Values are stored as basis points
    (percent * 100) for compact integer storage.
    """
    entry_kwargs = {
        "cpu_usage_percent": _to_basis_points(point.get("cpu_usage_percent", 0)),
        "memory_usage_percent": _to_basis_points(point.get("memory_usage_percent", 0)),
        "storage_usage_percent": _to_basis_points(point.get("storage_usage_percent", 0)),
        "network_rx_bytes": point.get("network_rx_bytes", 0),
        "network_tx_bytes": point.get("network_tx_bytes", 0),
        "disk_io_bytes": point.get("disk_io_bytes", 0),
        "running_instances": point.get("running_instances", 0),
        "nodes": point.get("nodes"),
        "scope": scope,
        "collected_at": _parse_timestamp(point.get("timestamp")),
    }

    for attempt in range(_MAX_RETRIES):
        try:
            async with async_session_maker() as db:
                db.add(MetricsSnapshot(**entry_kwargs))
                await db.commit()
                return
        except Exception:
            if attempt < _MAX_RETRIES - 1:
                wait = _RETRY_BACKOFF * (2 ** attempt)
                logger.warning(
                    "Metrics snapshot write attempt %d/%d failed, retrying in %.1fs",
                    attempt + 1, _MAX_RETRIES, wait, exc_info=True,
                )
                await asyncio.sleep(wait)
            else:
                logger.error(
                    "Metrics snapshot write FAILED after %d attempts — point lost",
                    _MAX_RETRIES, exc_info=True,
                )


# ----------------------------------------------
#  READ (used by history endpoint)
# ----------------------------------------------

async def load_metrics_history(
    points: int = 60,
    scope: str = "all",
) -> List[Dict[str, Any]]:
    """
    Load the most recent *points* snapshots from PostgreSQL.

    Returns dicts matching the MetricsHistoryPoint schema so the
    endpoint can wrap them in Pydantic models directly.

    For large datasets we push the LIMIT/ORDER BY to the database
    to avoid transferring unused rows.
    """
    try:
        async with async_session_maker() as db:
            # subquery: pick the N most recent rows (DESC), then re-order ASC
            stmt = (
                select(MetricsSnapshot)
                .where(MetricsSnapshot.scope == scope)
                .order_by(MetricsSnapshot.collected_at.desc())
                .limit(points)
            )
            result = await db.execute(stmt)
            rows = list(result.scalars().all())

        # reverse so oldest-first (chart x-axis)
        rows.reverse()

        return [
            {
                "timestamp": row.collected_at.isoformat(),
                "cpu_usage_percent": round(row.cpu_usage_percent / 100, 2),
                "memory_usage_percent": round(row.memory_usage_percent / 100, 2),
                "storage_usage_percent": round(row.storage_usage_percent / 100, 2),
                "network_rx_bytes": row.network_rx_bytes,
                "network_tx_bytes": row.network_tx_bytes,
                "disk_io_bytes": row.disk_io_bytes,
                "running_instances": row.running_instances,
                "nodes": row.nodes,
            }
            for row in rows
        ]
    except Exception as e:
        logger.error("Failed to load metrics history from DB: %s", e, exc_info=True)
        return []


async def get_history_count(scope: str = "all") -> int:
    """Return the total number of snapshots stored for a scope."""
    try:
        async with async_session_maker() as db:
            stmt = select(func.count()).select_from(MetricsSnapshot).where(
                MetricsSnapshot.scope == scope
            )
            result = await db.execute(stmt)
            return result.scalar() or 0
    except Exception:
        return 0


# ----------------------------------------------
#  RETENTION CLEANUP
# ----------------------------------------------

async def cleanup_old_snapshots(
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> int:
    """
    Delete snapshots older than *retention_days*.
    Returns the number of rows deleted.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    try:
        async with async_session_maker() as db:
            stmt = (
                delete(MetricsSnapshot)
                .where(MetricsSnapshot.collected_at < cutoff)
            )
            result = await db.execute(stmt)
            await db.commit()
            deleted = result.rowcount
            if deleted:
                logger.info(
                    "Metrics retention: deleted %d snapshots older than %d days",
                    deleted, retention_days,
                )
            return deleted
    except Exception as e:
        logger.error("Metrics retention cleanup failed: %s", e, exc_info=True)
        return 0


async def run_retention_loop(
    interval_hours: int = 6,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> None:
    """
    Background loop that periodically purges old snapshots.
    Designed to be started once via asyncio.create_task() at app startup.
    """
    logger.info(
        "Metrics retention loop started (every %dh, keep %d days)",
        interval_hours, retention_days,
    )
    while True:
        try:
            await asyncio.sleep(interval_hours * 3600)
            await cleanup_old_snapshots(retention_days)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Retention loop error: %s", e, exc_info=True)


# ----------------------------------------------
#  HELPERS
# ----------------------------------------------

def _to_basis_points(value: float) -> int:
    """Convert a percentage (ex; 2.08) to basis points (208) for integer storage."""
    return int(round(value * 100))


def _parse_timestamp(ts: Any) -> datetime:
    """Best-effort parse of an ISO string or datetime into tz-aware UTC."""
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            pass
    return datetime.now(timezone.utc)
