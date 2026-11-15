"""What binding an organization to a cluster would change, read before the bind.

A second organization on a cluster changes it for every holder: partners there are held to
`default` plus their own projects (org_scope.get_visible_projects), the write policy becomes the
shared one, and when the new organization is another party's the holders lose the state that
belongs to the whole cluster (core/whole_cluster_access.py). The preview answers from the real
party rules by adding the row inside a savepoint that is always rolled back, so it cannot drift
from what the bind does. The organizations bind route and the upgrade report share it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.shared_cluster_audit import audit_shared_cluster, owner_identity

logger = logging.getLogger(__name__)

# The holders whose loss of the whole cluster is checked; more than this is reported as unknown.
_RUNNERS_CHECKED = 25


class _Rollback(Exception):
    """Raised inside the savepoint so the trial row never stays."""


@dataclass
class OrganizationOnCluster:
    id: int
    name: str


@dataclass
class BindPreview:
    cluster_id: str
    organization_id: int
    allowed: bool
    refusal: Optional[str] = None
    binding_kind: Optional[str] = None  # "registered" (the caller's party holds it whole) or "granted" (admin)
    already_bound: Optional[bool] = None
    several_organizations_before: Optional[bool] = None
    several_organizations_after: Optional[bool] = None
    holders_lose_whole_cluster_tools: Optional[bool] = None
    organizations_on_cluster: Optional[List[OrganizationOnCluster]] = None
    share_audit: Optional[Dict[str, Any]] = None
    notes: List[str] = field(default_factory=list)


def refused(cluster_id: str, organization_id: int, reason: str) -> BindPreview:
    """A refused preview names the refusal and nothing else about the cluster."""
    return BindPreview(cluster_id=cluster_id, organization_id=organization_id, allowed=False, refusal=reason)


async def _holder_rows(db: AsyncSession, cluster_id: str) -> List[Tuple[int, Optional[int], Optional[str]]]:
    from app.models.db_models import OrganizationCluster

    rows = (await db.execute(
        select(OrganizationCluster.id, OrganizationCluster.organization_id, OrganizationCluster.registered_by)
        .where(OrganizationCluster.cluster_id == cluster_id)
    )).all()
    return [(row[0], row[1], row[2]) for row in rows]


async def _runners(db: AsyncSession, org_ids: Set[int], registrants: Set[str]) -> Set[str]:
    """The accounts that run the holder organizations (created, or a partner row), plus registrants."""
    from app.models.db_models import Organization, OrganizationMember
    from app.services.cluster_parties import RUNNING_ROLES

    emails = {r.lower() for r in registrants if r}
    if org_ids:
        rows = (await db.execute(
            select(Organization.created_by, OrganizationMember.user_email, OrganizationMember.role)
            .outerjoin(OrganizationMember, OrganizationMember.organization_id == Organization.id)
            .where(Organization.id.in_(sorted(org_ids)), Organization.is_active == True)  # noqa: E712
        )).all()
        for created_by, member, role in rows:
            if created_by and "@" in created_by:
                emails.add(created_by.lower())
            if member and role in RUNNING_ROLES:
                emails.add(member.lower())
    return emails


async def _held_whole_by(db: AsyncSession, emails: Set[str], cluster_id: str) -> Set[str]:
    from app.services.cluster_parties import clusters_held_by_another_party

    whole = set()
    for email in sorted(emails)[:_RUNNERS_CHECKED]:
        if cluster_id not in await clusters_held_by_another_party(db, email, {cluster_id}):
            whole.add(email)
    return whole


def _several(identities: Set[str]) -> bool:
    return len(identities) > 1


async def preview_bind(
    db: AsyncSession,
    *,
    organization_id: int,
    cluster_id: str,
    registrant: Optional[str],
    granter: Optional[str],
    show_organizations: bool,
    audit: bool = True,
) -> BindPreview:
    """The consequences of binding `organization_id` to `cluster_id` with these stamps.

    The caller has already passed the bind's own authorization (organizations.bind_decision).
    """
    from app.models.db_models import Organization, OrganizationCluster

    rows = await _holder_rows(db, cluster_id)
    preview = BindPreview(
        cluster_id=cluster_id,
        organization_id=organization_id,
        allowed=True,
        binding_kind="registered" if registrant else "granted",
        already_bound=any(org_id == organization_id for _id, org_id, _r in rows),
    )
    before = {owner_identity(org_id, registered_by, row_id) for row_id, org_id, registered_by in rows}
    prospective = owner_identity(organization_id, registrant)
    preview.several_organizations_before = _several(before)
    preview.several_organizations_after = _several(before | {prospective})

    holder_orgs = {org_id for _id, org_id, _r in rows if org_id is not None}
    if show_organizations:
        names = dict((await db.execute(
            select(Organization.id, Organization.name).where(Organization.id.in_(sorted(holder_orgs)))
        )).all()) if holder_orgs else {}
        preview.organizations_on_cluster = [OrganizationOnCluster(id=o, name=names.get(o, f"organization {o}"))
                                            for o in sorted(holder_orgs)]

    if preview.already_bound:
        preview.holders_lose_whole_cluster_tools = False
        return preview

    runners = await _runners(db, holder_orgs, {r for _id, _o, r in rows if r})
    if len(runners) > _RUNNERS_CHECKED:
        preview.notes.append(f"Only the first {_RUNNERS_CHECKED} holder accounts were checked for whole-cluster tools.")
    whole_before = await _held_whole_by(db, runners, cluster_id)
    whole_after: Set[str] = set()
    try:
        async with db.begin_nested():
            db.add(OrganizationCluster(organization_id=organization_id, cluster_id=cluster_id,
                                       registered_by=registrant, granted_by=None if registrant else granter))
            await db.flush()
            whole_after = await _held_whole_by(db, whole_before, cluster_id)
            raise _Rollback()
    except _Rollback:
        pass
    preview.holders_lose_whole_cluster_tools = bool(whole_before - whole_after)

    # The bind audits the cluster's own resources when it adds an owner to existing ones
    # (shared_cluster_audit.assert_cluster_safe_to_share); show that verdict now.
    if audit and before and prospective not in before:
        try:
            report = (await audit_shared_cluster(cluster_id)).as_dict()
            preview.share_audit = {key: report.get(key) for key in ("safe", "complete", "findings")}
        except Exception as exc:  # noqa: BLE001 - the bind runs the audit again and fails closed
            logger.warning("Bind preview could not audit %s: %s", cluster_id, type(exc).__name__)
            preview.share_audit = {"safe": None, "complete": False, "findings": []}
    return preview


async def bound_cluster_ids(db: AsyncSession, organization_id: int) -> Set[str]:
    from app.models.db_models import OrganizationCluster

    return set((await db.execute(
        select(OrganizationCluster.cluster_id).where(OrganizationCluster.organization_id == organization_id)
    )).scalars().all())


async def every_bound_cluster_id(db: AsyncSession) -> Set[str]:
    from app.models.db_models import OrganizationCluster

    return set((await db.execute(select(func.distinct(OrganizationCluster.cluster_id)))).scalars().all())


__all__ = [
    "BindPreview",
    "OrganizationOnCluster",
    "bound_cluster_ids",
    "every_bound_cluster_id",
    "preview_bind",
    "refused",
]
