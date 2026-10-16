"""Move a relocated project's Orcastra records to its new identity, and back.

On top of every instance's records (``metadata_cutover``), a project carries project-wide
tenant rules (``instance_name='*'``) and project tags. Only rules of the job organization's own
policies move; OrcaHub-synced policies are left for OrcaHub. Wildcard project rules
(``project_name='*'``) are cluster-wide and stay where they are.

Every rule stays in the policy that holds it, so the tenants a policy's assignments reach do not
change. Both directions run inside the caller's transaction.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import TagAssignment, TenantAccessPolicy, TenantPolicyRule

from .metadata_cutover import InstanceIdentity, move_instance_records


@dataclass(frozen=True)
class ProjectIdentity:
    cluster_id: str
    project: str


async def _move_project_rules(
    db: AsyncSession,
    old: ProjectIdentity,
    new: ProjectIdentity,
    *,
    organization_id: int,
) -> int:
    """Point project-wide rules at the new identity, each inside the policy that holds it.

    important: a rule never changes policy. A policy's assignments decide which tenants a rule
    reaches, so re-homing a rule (for example from ``dashboard:<old>`` to ``dashboard:<new>``)
    would hand the project to another policy's tenants (security review). A managed policy may
    therefore name a project on another cluster after a move; every reader matches rules by
    their own cluster and project, and a project delete removes them whatever policy holds them.
    """
    rows = (
        await db.execute(
            select(TenantPolicyRule)
            .join(TenantAccessPolicy, TenantAccessPolicy.id == TenantPolicyRule.policy_id)
            .where(
                TenantAccessPolicy.organization_id == organization_id,
                TenantAccessPolicy.source != "orcahub",
                TenantPolicyRule.cluster_id == old.cluster_id,
                TenantPolicyRule.project_name == old.project,
                TenantPolicyRule.instance_name == "*",
            )
        )
    ).scalars().all()
    moved = 0
    for rule in rows:
        existing = (
            await db.execute(
                select(TenantPolicyRule).where(
                    TenantPolicyRule.policy_id == rule.policy_id,
                    TenantPolicyRule.cluster_id == new.cluster_id,
                    TenantPolicyRule.project_name == new.project,
                    TenantPolicyRule.instance_name == "*",
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.permissions = sorted(
                set(existing.permissions or []) | set(rule.permissions or [])
            )
            await db.delete(rule)
        else:
            rule.cluster_id = new.cluster_id
            rule.project_name = new.project
        moved += 1
    await db.flush()
    return moved


async def _move_project_tags(
    db: AsyncSession, old: ProjectIdentity, new: ProjectIdentity
) -> int:
    match = and_(
        TagAssignment.resource_type == "project",
        TagAssignment.resource_id == old.project,
        TagAssignment.resource_host == old.cluster_id,
    )
    moving = {
        tag_id for tag_id in (await db.execute(select(TagAssignment.tag_id).where(match))).scalars()
    }
    if not moving:
        return 0
    stale = (
        await db.execute(
            select(TagAssignment).where(
                TagAssignment.resource_type == "project",
                TagAssignment.resource_id == new.project,
                TagAssignment.resource_host == new.cluster_id,
                TagAssignment.tag_id.in_(moving),
            )
        )
    ).scalars().all()
    for row in stale:
        await db.delete(row)
    await db.flush()
    result = await db.execute(
        update(TagAssignment)
        .where(match)
        .values(resource_id=new.project, resource_host=new.cluster_id)
    )
    return int(result.rowcount or 0)


async def move_project_records(
    db: AsyncSession,
    *,
    old: ProjectIdentity,
    new: ProjectIdentity,
    organization_id: int,
    instances: list[tuple[InstanceIdentity, InstanceIdentity]],
) -> dict[str, int]:
    """Point every record of the project and its instances at ``new``. Caller commits."""
    counts = {"tags": 0, "schedules": 0, "tenant_rules": 0, "project_rules": 0, "project_tags": 0}
    for before, after in instances:
        moved = await move_instance_records(
            db, old=before, new=after, organization_id=organization_id
        )
        for key, value in moved.items():
            counts[key] += value
    if (old.cluster_id, old.project) != (new.cluster_id, new.project):
        counts["project_rules"] = await _move_project_rules(
            db, old, new, organization_id=organization_id
        )
        counts["project_tags"] = await _move_project_tags(db, old, new)
    return counts


__all__ = ["ProjectIdentity", "move_project_records"]
