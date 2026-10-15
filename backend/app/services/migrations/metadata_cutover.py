"""Move Orcastra's per-instance records to an instance's new identity, and back.

Three stores are keyed by (cluster, project, instance): tag assignments, Orcastra snapshot
schedules, and exact tenant access rules. A staged move points them at the target in the same
database transaction that retains the source, and a rollback points them back. Both directions
run inside the caller's transaction and never commit.

Only rows owned by the job's organization move. OrcaHub-synced policies are left alone because
OrcaHub rewrites them on its next sync, and OrcaHub-provisioned projects cannot be moved.
Wildcard tenant rules are not instance records and stay as they are.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import and_, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import TagAssignment, TenantAccessPolicy, TenantPolicyRule
from app.models.snapshot_schedule_models import SnapshotSchedule


@dataclass(frozen=True)
class InstanceIdentity:
    cluster_id: str
    project: str
    name: str
    cluster_fingerprint: Optional[str] = None
    uuid: Optional[str] = None


async def _move_tags(db: AsyncSession, old: InstanceIdentity, new: InstanceIdentity) -> int:
    old_match = and_(
        TagAssignment.resource_type == "instance",
        TagAssignment.resource_id == old.name,
        TagAssignment.resource_host == old.cluster_id,
        TagAssignment.resource_project == old.project,
    )
    moving = {
        tag_id
        for tag_id in (await db.execute(select(TagAssignment.tag_id).where(old_match))).scalars()
    }
    if not moving:
        return 0
    # The new identity is either fresh or a rollback target: any row already there for the
    # same tag is the same assignment, so drop it before the identity update.
    await db.execute(
        delete(TagAssignment).where(
            TagAssignment.resource_type == "instance",
            TagAssignment.resource_id == new.name,
            TagAssignment.resource_host == new.cluster_id,
            TagAssignment.resource_project == new.project,
            TagAssignment.tag_id.in_(moving),
        )
    )
    result = await db.execute(
        update(TagAssignment)
        .where(old_match)
        .values(
            resource_id=new.name,
            resource_host=new.cluster_id,
            resource_project=new.project,
        )
    )
    return int(result.rowcount or 0)


async def _move_schedules(
    db: AsyncSession, old: InstanceIdentity, new: InstanceIdentity, organization_id: int
) -> int:
    rows = (
        await db.execute(
            select(SnapshotSchedule.id).where(
                SnapshotSchedule.cluster_id == old.cluster_id,
                SnapshotSchedule.project == old.project,
                SnapshotSchedule.instance_name == old.name,
                SnapshotSchedule.organization_id == organization_id,
            )
        )
    ).scalars().all()
    if not rows:
        return 0
    # note: one schedule per identity (unique cluster, project, name) whatever its organization.
    # The new identity has no instance on the daemon, so a row there is an orphan of a deleted
    # instance and must go, or the update below would violate the constraint mid-cutover.
    await db.execute(
        delete(SnapshotSchedule).where(
            SnapshotSchedule.cluster_id == new.cluster_id,
            SnapshotSchedule.project == new.project,
            SnapshotSchedule.instance_name == new.name,
            SnapshotSchedule.id.not_in(rows),
        )
    )
    result = await db.execute(
        update(SnapshotSchedule)
        .where(SnapshotSchedule.id.in_(rows))
        .values(
            cluster_id=new.cluster_id,
            project=new.project,
            instance_name=new.name,
            instance_uuid=new.uuid,
            cluster_fingerprint=new.cluster_fingerprint,
        )
    )
    return int(result.rowcount or 0)


async def _move_tenant_rules(
    db: AsyncSession, old: InstanceIdentity, new: InstanceIdentity, organization_id: int
) -> int:
    owned_policy = select(TenantAccessPolicy.id).where(
        TenantAccessPolicy.organization_id == organization_id,
        TenantAccessPolicy.source != "orcahub",
    )
    rules = (
        await db.execute(
            select(TenantPolicyRule).where(
                TenantPolicyRule.policy_id.in_(owned_policy),
                TenantPolicyRule.cluster_id == old.cluster_id,
                TenantPolicyRule.project_name == old.project,
                TenantPolicyRule.instance_name == old.name,
            )
        )
    ).scalars().all()
    moved = 0
    for rule in rules:
        existing = (
            await db.execute(
                select(TenantPolicyRule).where(
                    TenantPolicyRule.policy_id == rule.policy_id,
                    TenantPolicyRule.cluster_id == new.cluster_id,
                    TenantPolicyRule.project_name == new.project,
                    TenantPolicyRule.instance_name == new.name,
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
            rule.instance_name = new.name
        moved += 1
    return moved


async def move_instance_records(
    db: AsyncSession,
    *,
    old: InstanceIdentity,
    new: InstanceIdentity,
    organization_id: int,
) -> dict[str, int]:
    """Point every per-instance Orcastra record at ``new``. Runs in the caller's transaction."""
    if (old.cluster_id, old.project, old.name) == (new.cluster_id, new.project, new.name):
        return {"tags": 0, "schedules": 0, "tenant_rules": 0}
    return {
        "tags": await _move_tags(db, old, new),
        "schedules": await _move_schedules(db, old, new, organization_id),
        "tenant_rules": await _move_tenant_rules(db, old, new, organization_id),
    }


__all__ = ["InstanceIdentity", "move_instance_records"]
