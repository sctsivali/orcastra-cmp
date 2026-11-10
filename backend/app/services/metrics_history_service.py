"""Persistent metrics history in PostgreSQL.

Writes retry with backoff and the caller schedules them fire-and-forget, so the
background collector never waits on database I/O. Reads feed the chart endpoints.

Retention (7 days by default) is enforced by a periodic cleanup loop started at app
startup.
"""

import logging
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select, delete, func

from app.core.database import async_session_maker
from app.models.db_models import MetricsSnapshot

logger = logging.getLogger(__name__)

# 7 days of 30-second snapshots is about 20k rows.
DEFAULT_RETENTION_DAYS = 7

# Write retries, the same pattern as session_audit.py.
_MAX_RETRIES = 3
_RETRY_BACKOFF = 0.5


# ----------------------------------------------
#  WRITE (fire-and-forget, used by collector)
# ----------------------------------------------

async def persist_metrics_snapshot(
    point: Dict[str, Any],
    scope: str = "all",
) -> None:
    """Persist one metrics point to PostgreSQL, retrying with backoff.

    Percentages are stored as basis points (percent * 100) in integer columns.
    """
    # important: skip, never zero. The column is NOT NULL, and a point taken before any
    # instance had a second CPU sample would otherwise persist as an idle fleet.
    if point.get("cpu_usage_percent") is None:
        return
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
                    "Metrics snapshot write FAILED after %d attempts, point lost",
                    _MAX_RETRIES, exc_info=True,
                )


# ----------------------------------------------
#  READ (used by history endpoint)
# ----------------------------------------------

async def load_metrics_history(
    points: int = 60,
    scope: str = "all",
    since: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """The newest *points* snapshots, oldest first, from *since* on when given.

    Returns dicts matching the MetricsHistoryPoint schema. ORDER BY and LIMIT run in the
    database, so unused rows are never transferred.
    """
    try:
        async with async_session_maker() as db:
            # The N most recent rows, newest first; reversed below.
            stmt = select(MetricsSnapshot).where(MetricsSnapshot.scope == scope)
            if since is not None:
                stmt = stmt.where(MetricsSnapshot.collected_at >= since)
            stmt = stmt.order_by(MetricsSnapshot.collected_at.desc()).limit(points)
            result = await db.execute(stmt)
            rows = list(result.scalars().all())

        # reverse so oldest-first (chart x-axis)
        rows.reverse()

        from app.services.monitoring_history import node_rates

        points_out: List[Dict[str, Any]] = []
        for row in rows:
            # Newer rows keep the network rate in their per-Cluster entries and 0 in the
            # counter columns; older rows carry the counters.
            rx_rate, tx_rate = node_rates(row.nodes)
            points_out.append(
                {
                    "timestamp": row.collected_at.isoformat(),
                    "cpu_usage_percent": round(row.cpu_usage_percent / 100, 2),
                    "memory_usage_percent": round(row.memory_usage_percent / 100, 2),
                    "storage_usage_percent": round(row.storage_usage_percent / 100, 2),
                    "network_rx_bytes": row.network_rx_bytes,
                    "network_tx_bytes": row.network_tx_bytes,
                    "network_rx_bytes_per_sec": rx_rate,
                    "network_tx_bytes_per_sec": tx_rate,
                    "disk_io_bytes": row.disk_io_bytes,
                    "running_instances": row.running_instances,
                    "nodes": row.nodes,
                }
            )
        return points_out
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
    """Delete snapshots older than *retention_days*; returns the number deleted."""
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
    """Purge old snapshots every *interval_hours*; started once at app startup."""
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
