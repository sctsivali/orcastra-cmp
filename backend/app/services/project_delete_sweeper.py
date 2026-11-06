"""Finish the project deletes their requests could not: settle, revoke the captured rules, announce.

Every worker runs this loop. Rows are leased with FOR UPDATE SKIP LOCKED, at most one per cluster
per pass, so workers share the queue instead of racing for it and one silent cluster cannot hold
every probe (services/project_delete_store.claim_due).

important: a project is called gone only by an exact 404, read twice: once outside the cluster lock
to avoid taking it for nothing, then again under it, because a same-name create may have landed in
between. A project still there under the lock means the rules now belong to it; nothing is revoked.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, Set

from app.services import project_delete_announce as announcer
from app.services import project_delete_store as store
from app.services import project_grants
from app.services.lxd import health
from app.services.lxd.exceptions import LXDOperationUnknownError
from app.services.shared_cluster_audit import ClusterConfigurationBusy, serialize_cluster_configuration

logger = logging.getLogger(__name__)

TICK_SECONDS = 10.0
BATCH = 8
LOCK_WAIT_SECONDS = 30.0
RECHECK = timedelta(seconds=10)
PRUNE_EVERY_TICKS = 360

_task: Optional[asyncio.Task] = None


async def _exists(lxd_service: Any, host: str, name: str) -> Optional[bool]:
    if health.is_tripped(host):
        # A cluster the breaker has given up on would only hold a probe thread; wait for it.
        return None
    try:
        return await lxd_service.project_exists(host, name)
    except LXDOperationUnknownError:
        return None


async def finish(lxd_service: Any, followup: store.Followup, session_maker: Callable[[], Any]) -> str:
    """Take one leased row one step further; returns what happened, for the log and the tests."""
    if followup.state == "settled":
        await announcer.announce_and_finish(followup, settled_later=True)
        return "announced"
    if followup.deadline_at is not None and datetime.now(timezone.utc) >= followup.deadline_at:
        await store.expire(followup.id, "still present when the follow-up window ended")
        logger.warning(
            "Project %s on %s was still present when its delete follow-up ended; no rule was revoked",
            followup.project_name, followup.cluster_id,
        )
        return "expired"
    host, name = followup.cluster_id, followup.project_name
    held_by: Set[int] = set()
    if await _exists(lxd_service, host, name) is not False:
        await store.retry_later(followup.id, RECHECK)
        return "waiting"
    try:
        async with session_maker() as db:
            async with serialize_cluster_configuration(db, host, wait_seconds=LOCK_WAIT_SECONDS):
                present = await _exists(lxd_service, host, name)
                if present is None:
                    await store.retry_later(followup.id, RECHECK)
                    return "waiting"
                if present:
                    await store.expire(followup.id, "a project of the same name exists again")
                    logger.info("Project %s on %s exists again; the earlier delete revokes nothing", name, host)
                    return "superseded"
                await project_grants.forget_deleted_project(
                    db, cluster_id=host, project_name=name, only_rule_ids=followup.rule_ids, held_by=held_by
                )
    except ClusterConfigurationBusy:
        await store.retry_later(followup.id, RECHECK)
        return "waiting"
    # note: after the revoke committed. A crash before this leaves the row open, and the next pass
    # reads the 404 again and revokes the same ids again, which removes nothing more.
    await store.mark_settled(followup.id)
    announcer.clear_caches(followup.origin)
    logger.info("Project %s on %s finished deleting after its request answered", name, host)
    # note: attempts restart here, as mark_settled restarts them in the row. The claim counted every
    # probe pass, and carried into the announcement a first failure would have been its last.
    await announcer.announce_and_finish(
        replace(followup, state="settled", attempts=0), settled_later=True, reached_organizations=held_by
    )
    return "settled"


async def sweep_once(lxd_service: Any, session_maker: Callable[[], Any]) -> int:
    rows = await store.claim_due(BATCH)
    for followup in rows:
        try:
            await finish(lxd_service, followup, session_maker)
        except Exception as exc:  # noqa: BLE001 - one row must never stop the pass
            logger.warning(
                "Settling the delete of %s on %s failed: %s",
                followup.project_name, followup.cluster_id, exc, exc_info=True,
            )
            await store.retry_later(followup.id, RECHECK, error=f"{type(exc).__name__}: {exc}"[:500])
    return len(rows)


async def _loop(get_lxd_service: Callable[[], Any], session_maker: Callable[[], Any], interval: float) -> None:
    # Jittered so four workers started together do not claim in lockstep.
    await asyncio.sleep(random.uniform(0, interval))
    ticks = 0
    while True:
        try:
            await sweep_once(get_lxd_service(), session_maker)
            ticks += 1
            if ticks % PRUNE_EVERY_TICKS == 0:
                await store.prune()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a database blip must not end the loop
            logger.warning("Project delete sweep failed", exc_info=True)
        await asyncio.sleep(interval)


def start_project_delete_sweeper(
    get_lxd_service: Callable[[], Any], session_maker: Callable[[], Any], interval: float = TICK_SECONDS
) -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.get_running_loop().create_task(_loop(get_lxd_service, session_maker, interval))


async def stop_project_delete_sweeper() -> None:
    global _task
    task, _task = _task, None
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
