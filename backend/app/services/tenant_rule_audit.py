"""Report tenant access rules naming a project or instance that is gone from the cluster.

The dashboard forgets a rule whenever it deletes, renames or moves what the rule names
(services/project_deletion.py, instance_grants.py, stranded_rules.py). A delete done straight on
the cluster (`lxc delete`, `lxc project delete`) bypasses that, and the rule then hands the next
object created under that name to the rule's tenant. This loop only reports such rules; a Partner
or Administrator removes one from Tenant Access, which re-checks the cluster first
(routers/tenant_access.py).

important: a rule is reported only on evidence. A cluster's list counts only when it answered 200
with a list, a rule younger than a day or with a delete still settling is not judged, and a rule
must be missing on two complete reads at least half an interval apart. An unreachable or slow
cluster records nothing, so it never turns a live rule into a "missing" one.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import delete, select

from app.core.blocking import run_blocking
from app.models.db_models import TenantPolicyRule
from app.models.project_delete_models import OPEN_STATES, ProjectDeleteFollowup
from app.models.tenant_rule_models import TenantRuleObservation
from app.services.activity_logger import get_activity_logger
from app.services.collector_leadership import CollectorLease
from app.services.lxd import health

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

LEASE_KEY = "orcastra:tenant_rule_audit:lease"
MIN_RULE_AGE = timedelta(hours=24)
MISSES_TO_REPORT = 2
READ_TIMEOUT = (3, 10)
READ_WALL_SECONDS = 15.0
MAX_CLUSTERS_PER_PASS = 20

_task: Optional[asyncio.Task] = None


def _names(response: Any) -> Optional[Set[str]]:
    """The names in an LXD collection answer, or None when it is not a complete answer."""
    if getattr(response, "status_code", None) != 200:
        return None
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - an unreadable answer is no answer
        return None
    metadata = body.get("metadata") if isinstance(body, dict) else None
    if not isinstance(metadata, list):
        return None
    return {str(url).rstrip("/").split("/")[-1].split("?")[0] for url in metadata}


def _read_names(client: Any, collection: str, params: Optional[Dict[str, str]] = None) -> Optional[Set[str]]:
    response = client.api[collection].get(params=params or {}, timeout=READ_TIMEOUT)
    return _names(response)


async def _cluster_names(
    client: Any, cluster_id: str, projects_with_instance_rules: Iterable[str], executor: Any
) -> Tuple[Optional[Set[str]], Dict[str, Optional[Set[str]]]]:
    """(project names, {project: instance names}) or None where a read gave no complete answer."""

    async def read(path: str, params: Optional[Dict[str, str]] = None) -> Optional[Set[str]]:
        try:
            return await run_blocking(
                _read_names, client, path, params,
                timeout=READ_WALL_SECONDS, label=f"tenant_rule_audit[{cluster_id}]",
                executor=executor, max_in_flight=4,
            )
        except Exception:  # noqa: BLE001 - a failed read records nothing
            logger.debug("Rule audit read failed on %s %s", cluster_id, path, exc_info=True)
            return None

    projects = await read("projects")
    instances: Dict[str, Optional[Set[str]]] = {}
    if projects is not None:
        for project in sorted(set(projects_with_instance_rules) & projects):
            instances[project] = await read("instances", {"project": project})
    return projects, instances


def missing_rules(
    rules: Iterable[Any],
    projects: Optional[Set[str]],
    instances: Dict[str, Optional[Set[str]]],
) -> Tuple[Set[int], Set[int]]:
    """(rule ids judged missing, rule ids judged present). A rule not judged is in neither."""
    missing: Set[int] = set()
    present: Set[int] = set()
    if projects is None:
        return missing, present
    for rule in rules:
        if rule.project_name == "*":
            continue
        if rule.project_name not in projects:
            missing.add(rule.id)
            continue
        if rule.instance_name == "*":
            present.add(rule.id)
            continue
        names = instances.get(rule.project_name)
        if names is None:
            continue
        (present if rule.instance_name in names else missing).add(rule.id)
    return missing, present


async def audit_cluster(
    db: Any,
    cluster_id: str,
    client: Any,
    executor: Any,
    *,
    now: datetime,
    min_gap: timedelta,
) -> List[int]:
    """Judge one cluster's rules; returns the rule ids reported on this pass."""
    rules = (
        await db.execute(
            select(TenantPolicyRule).where(
                TenantPolicyRule.cluster_id == cluster_id,
                TenantPolicyRule.project_name != "*",
                TenantPolicyRule.created_at <= now - MIN_RULE_AGE,
            )
        )
    ).scalars().all()
    if not rules:
        return []
    settling = set(
        (
            await db.execute(
                select(ProjectDeleteFollowup.project_name).where(
                    ProjectDeleteFollowup.cluster_id == cluster_id,
                    ProjectDeleteFollowup.state.in_(OPEN_STATES),
                )
            )
        ).scalars().all()
    )
    rules = [r for r in rules if r.project_name not in settling]
    instance_projects = {r.project_name for r in rules if r.instance_name != "*"}
    projects, instances = await _cluster_names(client, cluster_id, instance_projects, executor)
    missing, present = missing_rules(rules, projects, instances)

    if present:
        await db.execute(delete(TenantRuleObservation).where(TenantRuleObservation.rule_id.in_(sorted(present))))
    observed = {
        row.rule_id: row
        for row in (
            await db.execute(select(TenantRuleObservation).where(TenantRuleObservation.rule_id.in_(sorted(missing) or [-1])))
        ).scalars().all()
    }
    reported: List[int] = []
    by_id = {r.id: r for r in rules}
    for rule_id in sorted(missing):
        row = observed.get(rule_id)
        if row is None:
            db.add(TenantRuleObservation(
                rule_id=rule_id, cluster_id=cluster_id, project_name=by_id[rule_id].project_name,
                instance_name=by_id[rule_id].instance_name, first_missing_at=now, last_missing_at=now, misses=1,
            ))
            continue
        last = row.last_missing_at if row.last_missing_at.tzinfo else row.last_missing_at.replace(tzinfo=timezone.utc)
        if now - last < min_gap:
            continue
        row.misses += 1
        row.last_missing_at = now
        if row.misses >= MISSES_TO_REPORT and row.reported_at is None:
            row.reported_at = now
            reported.append(rule_id)
    await db.commit()
    for rule_id in reported:
        rule = by_id[rule_id]
        activity_logger.log_activity(
            action="tenant.rule.orphan_detected",
            target_type="project",
            target_id=f"rule:{rule_id}",
            target_host=cluster_id,
            target_project=rule.project_name,
            result="success",
            details={"rule_id": rule_id, "policy_id": rule.policy_id, "instance": rule.instance_name},
        )
    return reported


async def audit_pass(
    session_maker: Callable[[], Any],
    client_for: Callable[[str], Any],
    executor: Any,
    *,
    interval: float,
    now: Optional[datetime] = None,
) -> List[int]:
    now = now or datetime.now(timezone.utc)
    reported: List[int] = []
    async with session_maker() as db:
        clusters = sorted(set(
            (
                await db.execute(
                    select(TenantPolicyRule.cluster_id).where(TenantPolicyRule.project_name != "*")
                )
            ).scalars().all()
        ))
        random.shuffle(clusters)
        for cluster_id in clusters[:MAX_CLUSTERS_PER_PASS]:
            if health.is_tripped(cluster_id):
                continue
            try:
                client = await asyncio.to_thread(client_for, cluster_id)
            except Exception:  # noqa: BLE001 - an unreachable cluster records nothing
                continue
            if client is None:
                continue
            try:
                reported += await audit_cluster(
                    db, cluster_id, client, executor, now=now, min_gap=timedelta(seconds=interval / 2)
                )
            except Exception:  # noqa: BLE001 - one cluster must not stop the pass
                await db.rollback()
                logger.warning("Rule audit failed on %s", cluster_id, exc_info=True)
    return reported


def _shared_redis() -> Any:
    from app.core.cache import get_cache_manager

    return getattr(get_cache_manager(), "redis_client", None)


async def _loop(session_maker: Callable[[], Any], client_for: Callable[[str], Any], interval: float) -> None:
    from concurrent.futures import ThreadPoolExecutor

    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="rule_audit_")
    lease = CollectorLease(LEASE_KEY, int(interval * 2 + 60), _shared_redis)
    await asyncio.sleep(random.uniform(30.0, 120.0))
    try:
        while True:
            try:
                if await asyncio.to_thread(lease.claim):
                    reported = await audit_pass(session_maker, client_for, executor, interval=interval)
                    if reported:
                        logger.warning("Tenant access rules naming nothing reported: %s", reported)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a blip must not end the loop
                logger.warning("Tenant rule audit pass failed", exc_info=True)
            await asyncio.sleep(interval + random.uniform(0.0, interval * 0.1))
    finally:
        try:
            lease.release()
        except Exception:  # noqa: BLE001
            pass
        executor.shutdown(wait=False)


def start_tenant_rule_audit(session_maker: Callable[[], Any], client_for: Callable[[str], Any], interval: float) -> None:
    global _task
    if interval <= 0:
        return
    if _task is None or _task.done():
        _task = asyncio.get_running_loop().create_task(_loop(session_maker, client_for, interval))


async def stop_tenant_rule_audit() -> None:
    global _task
    task, _task = _task, None
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
