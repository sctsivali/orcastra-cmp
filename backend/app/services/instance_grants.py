"""Forget the access rules that named one instance once that instance is gone.

A tenant rule can name a single instance: `(cluster, project, "web-01")`. Nothing removed such a
rule when its instance was deleted or renamed, so the next instance created under that name in
that project, by anyone, was visible and operable by the old tenant at once. This module removes
exactly those rules, the counterpart of `project_grants.forget_deleted_project` for one instance.

Only an exact `(cluster, project, instance)` match is touched. A rule for every instance of a
project (`*`) or for an instance name in every project (`project="*"`) describes no single
instance, so it outlives any one of them.

important: callers capture the rule ids before the delete goes out and forget only those once
LXD confirms the instance is gone, so a rule staged for a newcomer of the same name after the
delete is never taken. A failure here is reported and audited, never raised into the caller,
because the instance is already gone by then.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Collection, FrozenSet, Optional

from sqlalchemy import and_, delete, select

from app.models.db_models import TenantPolicyRule
from app.services.activity_logger import get_activity_logger

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

# note: a seam for tests, as in project_delete_store; the app's maker is used when unset.
session_maker: Optional[Callable[[], Any]] = None


def _maker() -> Callable[[], Any]:
    if session_maker is not None:
        return session_maker
    from app.core.database import async_session_maker

    return async_session_maker


def _exact(cluster_id: str, project_name: str, instance_name: str) -> Any:
    return and_(
        TenantPolicyRule.cluster_id == cluster_id,
        TenantPolicyRule.project_name == project_name,
        TenantPolicyRule.instance_name == instance_name,
    )


async def rule_ids_naming_instance(cluster_id: str, project_name: str, instance_name: str) -> FrozenSet[int]:
    """The ids of the rules naming exactly this instance. Empty on any read failure."""
    if not instance_name or instance_name == "*" or not project_name or project_name == "*":
        return frozenset()
    try:
        async with _maker()() as db:
            rows = await db.execute(
                select(TenantPolicyRule.id).where(_exact(cluster_id, project_name, instance_name))
            )
            return frozenset(rows.scalars().all())
    except Exception:  # noqa: BLE001 - a capture failure leaves the rules, it never blocks the delete
        logger.warning(
            "Could not read the access rules naming %s/%s on %s", project_name, instance_name, cluster_id,
            exc_info=True,
        )
        return frozenset()


async def forget_deleted_instance(
    *,
    cluster_id: str,
    project_name: str,
    instance_name: str,
    only_rule_ids: Optional[Collection[int]] = None,
    reason: str = "deleted",
) -> int:
    """Delete the rules naming this instance; returns how many went. Never raises."""
    if only_rule_ids is not None and not only_rule_ids:
        return 0
    condition = _exact(cluster_id, project_name, instance_name)
    if only_rule_ids is not None:
        condition = and_(condition, TenantPolicyRule.id.in_(sorted(only_rule_ids)))
    try:
        async with _maker()() as db:
            result = await db.execute(delete(TenantPolicyRule).where(condition))
            await db.commit()
            removed = int(result.rowcount or 0)
    except Exception as exc:  # noqa: BLE001 - the instance is gone; report what was left behind
        logger.error(
            "Could not remove the access rules naming %s/%s on %s: %s", project_name, instance_name, cluster_id, exc
        )
        activity_logger.log_activity(
            action="instance.access.revoke",
            target_type="instance",
            target_id=instance_name,
            target_host=cluster_id,
            target_project=project_name,
            result="failure",
            error_code=type(exc).__name__,
            error_message=str(exc),
            details={"reason": reason},
        )
        return 0
    if removed:
        activity_logger.log_activity(
            action="instance.access.revoke",
            target_type="instance",
            target_id=instance_name,
            target_host=cluster_id,
            target_project=project_name,
            result="success",
            details={"reason": reason, "rules_removed": removed},
        )
    return removed
