"""Tenant access rules that name nothing, reported by the audit loop, for a partner to remove.

The loop (services/tenant_rule_audit.py) only reports. Removal is a person's decision, made here,
and only after the cluster is read again under its configuration lock: a project or instance that
came back since the report keeps its rule.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthenticatedUser, get_current_user
from app.core.database import get_db
from app.core.dependencies import check_read_only_mode, require_partner
from app.core.org_scope import get_user_org_ids, org_scope_dependency, verify_cluster_access
from app.core.rbac import Role, get_highest_role, resolve_roles
from app.models.db_models import Organization, TenantAccessPolicy, TenantPolicyRule
from app.models.tenant_rule_models import TenantRuleObservation
from app.routers.tenant_access import _invalidate_acl_caches, _verify_org_access
from app.services import project_grants, tenant_rule_audit
from app.services.activity_logger import get_activity_logger
from app.services.lxd_service import LXDService, get_lxd_service
from app.services.shared_cluster_audit import ClusterConfigurationBusy, serialize_cluster_configuration

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="rule_orphan_")

router = APIRouter(
    prefix="/tenant-access",
    tags=["Tenant Access Policies"],
    dependencies=[Depends(require_partner()), Depends(org_scope_dependency)],
)


class OrphanRule(BaseModel):
    rule_id: int
    policy_id: int
    policy_name: str
    organization_id: int
    organization_name: str
    cluster_id: str
    project_name: str
    instance_name: str
    first_missing_at: datetime
    reported_at: datetime


class OrphanRuleList(BaseModel):
    rules: List[OrphanRule]
    total: int


@router.get("/orphan-rules", response_model=OrphanRuleList, summary="Access rules that name nothing")
async def list_orphan_rules(
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OrphanRuleList:
    """Rules the audit found naming a project or instance that is gone, in the caller's organizations."""
    stmt = (
        select(TenantRuleObservation, TenantPolicyRule, TenantAccessPolicy, Organization)
        .join(TenantPolicyRule, TenantPolicyRule.id == TenantRuleObservation.rule_id)
        .join(TenantAccessPolicy, TenantAccessPolicy.id == TenantPolicyRule.policy_id)
        .join(Organization, Organization.id == TenantAccessPolicy.organization_id)
        .where(TenantRuleObservation.reported_at.is_not(None))
        .order_by(TenantRuleObservation.first_missing_at)
    )
    if get_highest_role(resolve_roles(user)) != Role.ADMIN:
        stmt = stmt.where(Organization.id.in_(await get_user_org_ids(user, db) or [-1]))
    rows = (await db.execute(stmt)).all()
    rules = [
        OrphanRule(
            rule_id=rule.id, policy_id=policy.id, policy_name=policy.name, organization_id=org.id,
            organization_name=org.name, cluster_id=rule.cluster_id, project_name=rule.project_name,
            instance_name=rule.instance_name, first_missing_at=seen.first_missing_at, reported_at=seen.reported_at,
        )
        for seen, rule, policy, org in rows
    ]
    return OrphanRuleList(rules=rules, total=len(rules))


@router.delete(
    "/orphan-rules/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove an access rule that names nothing",
    dependencies=[Depends(check_read_only_mode)],
)
async def remove_orphan_rule(
    rule_id: int,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    lxd_service: LXDService = Depends(get_lxd_service),
) -> None:
    """Remove a reported rule after reading the cluster again: 409 if what it names is back."""
    seen = await db.get(TenantRuleObservation, rule_id)
    rule = await db.get(TenantPolicyRule, rule_id)
    if seen is None or seen.reported_at is None or rule is None:
        raise HTTPException(status_code=404, detail="No reported rule with this id")
    policy = await db.get(TenantAccessPolicy, rule.policy_id)
    if policy is None:
        raise HTTPException(status_code=404, detail="No reported rule with this id")
    await _verify_org_access(user, policy.organization_id, db, require_write=True)
    verify_cluster_access(rule.cluster_id)

    try:
        async with serialize_cluster_configuration(db, rule.cluster_id, wait_seconds=5.0):
            missing = await _still_missing(lxd_service, rule)
            if missing is None:
                raise HTTPException(
                    status_code=503,
                    detail=f"{rule.cluster_id} did not answer completely, so the rule was kept. Try again shortly.",
                )
            if not missing:
                raise HTTPException(
                    status_code=409,
                    detail="What this rule names exists again on the cluster, so the rule was kept.",
                )
            outcome = await project_grants.revoke_project_grants(
                db, cluster_id=rule.cluster_id, project_name=rule.project_name, only_rule_ids=[rule_id]
            )
    except ClusterConfigurationBusy as busy:
        raise HTTPException(
            status_code=409, detail=f"Another configuration change on {rule.cluster_id} is running. Retry shortly."
        ) from busy
    _invalidate_acl_caches()
    activity_logger.log_activity(
        action="tenant.rule.remove",
        target_type="project",
        target_id=f"rule:{rule_id}",
        target_host=rule.cluster_id,
        target_project=rule.project_name,
        target_organization_id=policy.organization_id,
        result="success",
        details={"policy": policy.name, "instance": rule.instance_name, "rules_removed": outcome.rules_removed},
    )


async def _still_missing(lxd_service: LXDService, rule: TenantPolicyRule) -> Optional[bool]:
    """True when the cluster says the project or instance is gone, None when it did not say."""
    try:
        client = await asyncio.to_thread(lxd_service.connection_manager.get_client, rule.cluster_id)
    except Exception:  # noqa: BLE001 - no answer keeps the rule
        return None
    if client is None:
        return None
    instance_projects = [rule.project_name] if rule.instance_name != "*" else []
    projects, instances = await tenant_rule_audit._cluster_names(client, rule.cluster_id, instance_projects, _executor)
    missing, present = tenant_rule_audit.missing_rules([rule], projects, instances)
    if rule.id in missing:
        return True
    if rule.id in present:
        return False
    return None
