"""Record which organization a dashboard-created project belongs to, and forget it on delete.

On a cluster bound to more than one organization a Partner is held to the default project
plus the projects their organizations' tenant access policies name (`get_visible_projects`).
A Partner may not write such a rule for a project they cannot already see
(`routers/tenant_access.py`), which left a project a Partner had just created outside their
own bound. This module writes that rule for them, and only for a project the request brought
into existence: LXD refuses a duplicate name before anything here runs, so the name a rule
carries cannot belong to another organization.

One policy per (organization, cluster), named `dashboard:<cluster>` with `source="local"`,
holds one rule per project. No assignment is written: the rule feeds the Partner bound, it
grants no tenant anything; `["view"]` is the least a later assignment could widen to.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_key_capabilities import is_valid_project_name
from app.core.org_scope import _visible_projects_var
from app.models.db_models import TenantAccessPolicy, TenantPolicyAssignment, TenantPolicyRule
from app.services.activity_logger import get_activity_logger

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

MANAGED_POLICY_PREFIX = "dashboard:"
MANAGED_POLICY_SOURCE = "local"
GRANT_PERMISSIONS = ["view"]
_POLICY_NAME_MAX = 100

@dataclass(frozen=True)
class GrantOutcome:
    organization_id: int
    policy_id: int
    policy_created: bool
    rule_created: bool

@dataclass(frozen=True)
class RevokeOutcome:
    rules_removed: int
    policies_dropped: int

def managed_policy_name(cluster_id: str) -> str:
    name = f"{MANAGED_POLICY_PREFIX}{cluster_id}"
    if len(name) > _POLICY_NAME_MAX:
        # A cluster id is a Vault key and can be long; keep the prefix and the tail, which is
        # the part that tells two long ids apart.
        keep = _POLICY_NAME_MAX - len(MANAGED_POLICY_PREFIX) - 1
        name = f"{MANAGED_POLICY_PREFIX}~{cluster_id[-keep:]}"
    return name

async def grant_project_to_creator(
    db: AsyncSession,
    *,
    organization_id: int,
    cluster_id: str,
    project_name: str,
    created_by: str,
) -> GrantOutcome:
    """Name `project_name` on `cluster_id` in the organization's managed policy. Idempotent."""
    if project_name in ("", "*", "default") or not is_valid_project_name(project_name):
        raise ValueError(f"not a project a grant may name: {project_name!r}")

    name = managed_policy_name(cluster_id)
    policy = (
        await db.execute(
            select(TenantAccessPolicy).where(
                and_(
                    TenantAccessPolicy.organization_id == organization_id,
                    TenantAccessPolicy.name == name,
                )
            )
        )
    ).scalar_one_or_none()
    policy_created = False
    if policy is None:
        policy = TenantAccessPolicy(
            organization_id=organization_id,
            name=name,
            description=(
                f"Projects created on {cluster_id} from the dashboard by this organization. One "
                "rule per project; a project's rule is removed when the project is deleted."
            ),
            is_default=False,
            source=MANAGED_POLICY_SOURCE,
            created_by=created_by,
        )
        db.add(policy)
        await db.flush()
        policy_created = True

    # Select then insert, the house pattern: a second call for the same project is a no-op
    # against the (policy, cluster, project, instance) unique constraint.
    rule = (
        await db.execute(
            select(TenantPolicyRule).where(
                and_(
                    TenantPolicyRule.policy_id == policy.id,
                    TenantPolicyRule.cluster_id == cluster_id,
                    TenantPolicyRule.project_name == project_name,
                    TenantPolicyRule.instance_name == "*",
                )
            )
        )
    ).scalar_one_or_none()
    rule_created = False
    if rule is None:
        db.add(
            TenantPolicyRule(
                policy_id=policy.id,
                cluster_id=cluster_id,
                project_name=project_name,
                instance_name="*",
                permissions=list(GRANT_PERMISSIONS),
            )
        )
        rule_created = True
    await db.commit()
    return GrantOutcome(
        organization_id=organization_id,
        policy_id=policy.id,
        policy_created=policy_created,
        rule_created=rule_created,
    )

async def revoke_project_grants(db: AsyncSession, *, cluster_id: str, project_name: str) -> RevokeOutcome:
    """Remove every rule naming (cluster, project), whichever organization wrote it.

    important: every organization, not the deleter's. Organization A's leftover rule for a
    project it deleted would otherwise put organization B's future project of the same name
    inside A's bound. A managed policy left with no rules and no assignments is dropped; an
    OrcaHub-owned policy keeps its row, OrcaHub upserts it by `external_id`.
    """
    rules = (
        await db.execute(
            select(TenantPolicyRule).where(
                and_(
                    TenantPolicyRule.cluster_id == cluster_id,
                    TenantPolicyRule.project_name == project_name,
                )
            )
        )
    ).scalars().all()
    touched = {rule.policy_id for rule in rules}
    for rule in rules:
        await db.delete(rule)
    await db.flush()

    dropped = 0
    for policy_id in sorted(touched):
        policy = (
            await db.execute(select(TenantAccessPolicy).where(TenantAccessPolicy.id == policy_id))
        ).scalar_one_or_none()
        if policy is None or not policy.name.startswith(MANAGED_POLICY_PREFIX):
            continue
        if policy.source != MANAGED_POLICY_SOURCE:
            continue
        left = (
            await db.execute(select(TenantPolicyRule.id).where(TenantPolicyRule.policy_id == policy_id))
        ).scalars().all()
        assigned = (
            await db.execute(
                select(TenantPolicyAssignment.id).where(TenantPolicyAssignment.policy_id == policy_id)
            )
        ).scalars().all()
        if not left and not assigned:
            await db.delete(policy)
            dropped += 1
    await db.commit()
    return RevokeOutcome(rules_removed=len(rules), policies_dropped=dropped)


async def record_created_project(
    db: AsyncSession, *, organization_id: int, cluster_id: str, project_name: str, created_by: str
) -> str:
    """Record the project in the creator's organization; a failure is reported, never a 500.

    The LXD project exists at this point. Answering 500 would make the UI retry a create that
    now meets a 409, so the outcome is audited and appended to the response message instead.
    """
    try:
        outcome = await grant_project_to_creator(
            db, organization_id=organization_id, cluster_id=cluster_id, project_name=project_name, created_by=created_by,
        )
    except Exception as e:  # noqa: BLE001 - the project exists; say so rather than fail the create
        logger.error(f"Could not record project {project_name} on {cluster_id} for organization {organization_id}: {e}")
        activity_logger.log_activity(
            action="project.access.grant",
            target_type="project",
            target_id=project_name,
            target_host=cluster_id,
            target_organization_id=organization_id,
            result="failure",
            error_code=type(e).__name__,
            error_message=str(e),
        )
        return (
            ". Access to it could not be recorded; ask an administrator to add it to your "
            "organization's policy"
        )
    widen_bound_in_request(cluster_id, project_name)
    activity_logger.log_activity(
        action="project.access.grant",
        target_type="project",
        target_id=project_name,
        target_host=cluster_id,
        target_organization_id=organization_id,
        result="success",
        details={
            "policy_id": outcome.policy_id,
            "policy_created": outcome.policy_created,
            "rule_created": outcome.rule_created,
        },
    )
    return ""


async def forget_deleted_project(db: AsyncSession, *, cluster_id: str, project_name: str) -> str:
    """Forget every rule that named the deleted project; a failure is reported, never a 500."""
    try:
        outcome = await revoke_project_grants(db, cluster_id=cluster_id, project_name=project_name)
    except Exception as e:  # noqa: BLE001 - the project is gone; say what was left behind
        logger.error(f"Could not remove the access rules naming {project_name} on {cluster_id}: {e}")
        activity_logger.log_activity(
            action="project.access.revoke",
            target_type="project",
            target_id=project_name,
            target_host=cluster_id,
            result="failure",
            error_code=type(e).__name__,
            error_message=str(e),
        )
        return ". An access rule naming it could not be removed and remains in a policy"
    narrow_bound_in_request(cluster_id, project_name)
    activity_logger.log_activity(
        action="project.access.revoke",
        target_type="project",
        target_id=project_name,
        target_host=cluster_id,
        result="success",
        details={"rules_removed": outcome.rules_removed, "policies_dropped": outcome.policies_dropped},
    )
    return ""


def _adjust_bound(cluster_id: str, project_name: str, add: bool) -> None:
    mapping = _visible_projects_var.get(None)
    if not mapping or cluster_id not in mapping:
        # A cluster absent from the map is unrestricted and must stay absent.
        return
    copy = {c: set(p) for c, p in mapping.items()}
    if add:
        copy[cluster_id].add(project_name)
    else:
        copy[cluster_id].discard(project_name)
    _visible_projects_var.set(copy)

def widen_bound_in_request(cluster_id: str, project_name: str) -> None:
    """Make the project visible to the rest of this request, as the next request will see it."""
    _adjust_bound(cluster_id, project_name, add=True)

def narrow_bound_in_request(cluster_id: str, project_name: str) -> None:
    _adjust_bound(cluster_id, project_name, add=False)

__all__ = [
    "GrantOutcome",
    "RevokeOutcome",
    "grant_project_to_creator",
    "revoke_project_grants",
    "record_created_project",
    "forget_deleted_project",
    "widen_bound_in_request",
    "narrow_bound_in_request",
    "managed_policy_name",
    "MANAGED_POLICY_PREFIX",
]
