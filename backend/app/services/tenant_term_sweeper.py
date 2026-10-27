"""Record each paid-term boundary once, as it passes, and clear what the tenant sees.

Enforcement does not wait for this: the grant query reads the dates on every request
(core/org_scope.py), so a policy past its term grants viewing only the moment the term ends. What
needs a loop is the trail. Nobody calls the dashboard at the instant a term ends, so without this
the audit would never say when a buyer's access narrowed or ended, and cached lists built
under the old grants would live out their TTL.

important: the phase is advanced with a conditional UPDATE (`IS DISTINCT FROM`) that returns the
rows it changed, so with two workers, or Redis down and every worker running the loop, each
boundary is still recorded exactly once.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional, Tuple

from sqlalchemy import or_, select, update

from app.core.tenant_term import ENDED, READ_ONLY, policy_phase
from app.models.db_models import TenantAccessPolicy, TenantPolicyRule
from app.services.activity_logger import get_activity_logger
from app.services.collector_leadership import CollectorLease

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

LEASE_KEY = "orcastra:tenant_term:lease"
INTERVAL_SECONDS = 60.0

_AUDITED = {READ_ONLY: "tenant.access.read_only", ENDED: "tenant.access.expire"}
_task: Optional[asyncio.Task] = None


def _shared_redis() -> Any:
    from app.core.cache import get_cache_manager

    return getattr(get_cache_manager(), "redis_client", None)


async def sweep(session_maker: Callable[[], Any], *, now: Optional[datetime] = None) -> List[Tuple[int, str]]:
    """Advance every dated policy whose phase moved; returns (policy id, new phase) for each."""
    now = now or datetime.now(timezone.utc)
    moved: List[Tuple[int, str]] = []
    async with session_maker() as db:
        rows = (
            await db.execute(
                select(
                    TenantAccessPolicy.id,
                    TenantAccessPolicy.organization_id,
                    TenantAccessPolicy.name,
                    TenantAccessPolicy.source,
                    TenantAccessPolicy.term_ended_at,
                    TenantAccessPolicy.access_ends_at,
                    TenantAccessPolicy.term_phase_recorded,
                ).where(
                    or_(TenantAccessPolicy.term_ended_at.is_not(None), TenantAccessPolicy.access_ends_at.is_not(None))
                )
            )
        ).all()
        for policy_id, org_id, name, source, term_ended_at, access_ends_at, recorded in rows:
            state = policy_phase(
                source=source, term_ended_at=term_ended_at, access_ends_at=access_ends_at,
                permissions=frozenset({"view"}), now=now,
            )
            if state.phase == recorded:
                continue
            changed = (
                await db.execute(
                    update(TenantAccessPolicy)
                    .where(
                        TenantAccessPolicy.id == policy_id,
                        TenantAccessPolicy.term_phase_recorded.is_distinct_from(state.phase),
                    )
                    # note: bookkeeping, not an edit: the policy's own updated_at stays as it was.
                    .values(term_phase_recorded=state.phase, updated_at=TenantAccessPolicy.updated_at)
                    .returning(TenantAccessPolicy.id)
                )
            ).scalar_one_or_none()
            await db.commit()
            if changed is None:
                continue  # another worker recorded it first
            moved.append((policy_id, state.phase))
            action = _AUDITED.get(state.phase)
            if action is None:
                continue
            target = (
                await db.execute(
                    select(TenantPolicyRule.cluster_id, TenantPolicyRule.project_name)
                    .where(TenantPolicyRule.policy_id == policy_id)
                    .limit(1)
                )
            ).first()
            activity_logger.log_activity(
                action=action,
                target_type="project",
                target_id=name,
                target_host=target[0] if target else None,
                target_project=target[1] if target else None,
                target_organization_id=org_id,
                result="success",
                details={
                    "term_ended_at": state.term_ended_at.isoformat() if state.term_ended_at else None,
                    "access_ends_at": state.access_ends_at.isoformat() if state.access_ends_at else None,
                },
            )
    if moved:
        from app.core.cache import get_cache_manager

        try:
            get_cache_manager().invalidate_after_access_change()
        except Exception:  # noqa: BLE001 - the scoped keys already follow the grant
            logger.warning("Could not clear access caches after a term boundary", exc_info=True)
    return moved


async def _loop(session_maker: Callable[[], Any]) -> None:
    lease = CollectorLease(LEASE_KEY, int(INTERVAL_SECONDS * 2 + 15), _shared_redis)
    await asyncio.sleep(random.uniform(5.0, 20.0))
    try:
        while True:
            try:
                if await asyncio.to_thread(lease.claim):
                    moved = await sweep(session_maker)
                    if moved:
                        logger.info("Tenant term boundaries recorded: %s", moved)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a blip must not end the loop
                logger.warning("Tenant term sweep failed", exc_info=True)
            await asyncio.sleep(INTERVAL_SECONDS + random.uniform(0.0, 10.0))
    finally:
        try:
            lease.release()
        except Exception:  # noqa: BLE001
            pass


def start_tenant_term_sweeper(session_maker: Callable[[], Any]) -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.get_running_loop().create_task(_loop(session_maker))


async def stop_tenant_term_sweeper() -> None:
    global _task
    task, _task = _task, None
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
