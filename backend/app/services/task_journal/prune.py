"""Retention for the task journal: every six hours, one worker deletes what is older than the
configured days.

Every worker runs the loop; a transaction-level advisory lock decides which one does the work
this pass, so a worker that dies mid-delete releases it with its transaction and the next pass
on any worker picks up. Deletes go in bounded batches so a month of rows never becomes one
long lock.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Callable, Optional, Tuple

from sqlalchemy import text

from app.core.config import get_settings
from app.services.task_journal import repository
from app.services.task_journal.repository_types import PRUNE_LOCK

logger = logging.getLogger(__name__)

FIRST_RUN_DELAY_S = 300.0
BATCH = 5000
INTERVAL_S = 6 * 3600.0
JITTER_S = 600.0
ACTION_PRUNE = "task.journal.prune"

_task: Optional[asyncio.Task] = None


async def prune_once(session_maker: Callable[[], Any]) -> Optional[Tuple[int, int]]:
    """One pass under the xact lock. None when another worker holds it this pass."""
    settings = get_settings()
    task_days = int(settings.task_journal_retention_days)
    log_days = int(settings.cluster_log_retention_days)
    key_days = int(settings.my_keys_audit_retention_days)
    # One transaction per batch, the xact lock re-taken each time: a month of rows is never
    # one long lock, and a worker that dies mid-pass leaves the next pass its remainder.
    tasks = logs = 0
    first = True
    while True:
        async with session_maker() as db:
            result = await db.execute(
                text("SELECT pg_try_advisory_xact_lock(:classid, :objid)"),
                {"classid": PRUNE_LOCK[0], "objid": PRUNE_LOCK[1]},
            )
            if not bool(result.scalar()):
                await db.rollback()
                return None if first else (tasks, logs)
            if first:
                await repository.mark_stale_open_rows_lost(db, task_days=task_days)
                await _roll_up_key_usage(db, log_days=log_days, task_days=task_days)
            t, l, more = await repository.prune_batch(
                db, task_days=task_days, log_days=log_days, batch=BATCH, key_days=key_days
            )
            await db.commit()
        first = False
        tasks += t
        logs += l
        if not more:
            break
    _audit(tasks=tasks, logs=logs, task_days=task_days, log_days=log_days)
    logger.info("Task journal pruned: %d tasks, %d log lines", tasks, logs)
    return tasks, logs


async def _roll_up_key_usage(db: Any, *, log_days: int, task_days: int) -> None:
    # note: under a savepoint, so a registry the database does not have yet (a rolling upgrade)
    # costs the roll-up and never the prune.
    try:
        async with db.begin_nested():
            rolled = await repository.roll_up_key_usage(db, log_days=log_days, task_days=task_days)
        if rolled:
            logger.info("My Keys last use carried past the prune for %d keys", rolled)
    except Exception:  # noqa: BLE001
        logger.warning("Could not carry My Keys last use past the prune", exc_info=True)


def _audit(**details: Any) -> None:
    try:
        from app.services.activity_logger import get_activity_logger

        get_activity_logger().log_activity(
            action=ACTION_PRUNE,
            target_type="system",
            target_id="task_journal",
            details=details,
        )
    except Exception:  # noqa: BLE001 - a failed audit line must not fail the prune
        logger.warning("Could not write the audit record for %s", ACTION_PRUNE, exc_info=True)


def _next_delay() -> float:
    return INTERVAL_S + random.uniform(0, JITTER_S)


async def prune_loop(session_maker: Callable[[], Any]) -> None:
    await asyncio.sleep(FIRST_RUN_DELAY_S)
    while True:
        try:
            await prune_once(session_maker)
        except asyncio.CancelledError:
            break
        except Exception:  # noqa: BLE001
            logger.error("Task journal prune pass failed", exc_info=True)
        try:
            await asyncio.sleep(_next_delay())
        except asyncio.CancelledError:
            break


def start_prune_loop(session_maker: Callable[[], Any]) -> None:
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(prune_loop(session_maker), name="task-journal-prune")


def stop_prune_loop() -> None:
    global _task
    if _task is not None and not _task.done():
        _task.cancel()
    _task = None
