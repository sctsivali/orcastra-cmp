"""A purchase's paid term, applied to everything the purchase reaches, not only to its own rules.

Provisioning binds the seller's cluster to the buyer's organization with no registrant
(`organization_clusters.registered_by IS NULL` on an OrcaHub organization): a sale-only binding.
The buyer reaches that cluster only because it bought something there, so its reach follows the
purchase's term, which lives on the OrcaHub policies of that organization naming the cluster:

  active     a live OrcaHub policy on the cluster whose paid term has not ended
  read_only  every live OrcaHub policy there is past its paid term (retention): view only
  ended      no live OrcaHub policy there: nothing

core/tenant_term.py clamps the OrcaHub policies themselves. This module carries the same phase to
what the purchase reaches indirectly: a buyer founder OrcaHub promoted to role_partner (no tenant
ACL at all), and a local policy a founder wrote in the buyer organization, which would otherwise
outlive the purchase. A binding someone registered, a local organization and the organization's
creator are not sale-only and are untouched.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Iterable, Optional, Set, Tuple

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

ACTIVE, READ_ONLY, ENDED = "active", "read_only", "ended"
Term = Tuple[Optional[datetime], Optional[datetime]]


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def sale_phase(terms: Iterable[Term], now: datetime) -> str:
    """The phase of a purchase from the (term_ended_at, access_ends_at) of its OrcaHub policies."""
    live = [(_aware(t), _aware(a)) for t, a in terms if a is None or _aware(a) > now]
    if not live:
        return ENDED
    if any(t is None or t > now for t, _a in live):
        return ACTIVE
    return READ_ONLY


def latest_access_end(terms: Iterable[Term]) -> Optional[datetime]:
    ends = [_aware(a) for _t, a in terms]
    return None if any(a is None for a in ends) or not ends else max(ends)


async def orcahub_terms(
    db: AsyncSession, org_ids: Iterable[int],
) -> Dict[Tuple[int, str], list]:
    """(organization, cluster) -> the terms of that organization's OrcaHub policies naming the cluster."""
    from app.models.db_models import TenantAccessPolicy, TenantPolicyRule

    ids = sorted(set(org_ids))
    if not ids:
        return {}
    rows = (await db.execute(
        select(
            TenantAccessPolicy.organization_id,
            TenantPolicyRule.cluster_id,
            TenantAccessPolicy.term_ended_at,
            TenantAccessPolicy.access_ends_at,
        )
        .join(TenantPolicyRule, TenantPolicyRule.policy_id == TenantAccessPolicy.id)
        .where(TenantAccessPolicy.organization_id.in_(ids), TenantAccessPolicy.source == "orcahub")
    )).all()
    out: Dict[Tuple[int, str], list] = {}
    for org_id, cluster_id, term_ended_at, access_ends_at in rows:
        out.setdefault((org_id, cluster_id), []).append((term_ended_at, access_ends_at))
    return out


async def _reach(db: AsyncSession, email: str) -> Tuple[Set[str], Dict[str, Set[int]]]:
    """The clusters this account reaches outright, and those it reaches only by purchase (with whose)."""
    from app.models.db_models import Organization, OrganizationCluster, OrganizationMember

    lower = email.lower()
    rows = (await db.execute(
        select(
            OrganizationCluster.cluster_id,
            OrganizationCluster.organization_id,
            OrganizationCluster.registered_by,
            Organization.source,
            Organization.created_by,
        )
        .join(Organization, Organization.id == OrganizationCluster.organization_id)
        .outerjoin(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .where(
            Organization.is_active == True,  # noqa: E712 - SQLAlchemy comparison
            or_(func.lower(OrganizationMember.user_email) == lower, func.lower(Organization.created_by) == lower),
        )
        .distinct()
    )).all()
    full: Set[str] = set((await db.execute(
        select(OrganizationCluster.cluster_id).where(func.lower(OrganizationCluster.registered_by) == lower)
    )).scalars().all())
    sale_only: Dict[str, Set[int]] = {}
    for cluster_id, org_id, registered_by, source, created_by in rows:
        if registered_by is not None or source != "orcahub" or (created_by or "").lower() == lower:
            full.add(cluster_id)
        else:
            sale_only.setdefault(cluster_id, set()).add(org_id)
    return full, {c: orgs for c, orgs in sale_only.items() if c not in full}


async def partner_read_only_clusters(db: AsyncSession, email: Optional[str]) -> Set[str]:
    """Clusters this account reaches only through purchases that are all past their paid term.

    For a role_partner account: on these, it may look but not change anything (owner decision
    2026-10-27: during retention the buyer has view only).
    """
    if not email:
        return set()
    _full, candidates = await _reach(db, email)
    if not candidates:
        return set()
    terms = await orcahub_terms(db, {o for orgs in candidates.values() for o in orgs})
    now = datetime.now(timezone.utc)
    read_only = set()
    for cluster_id, orgs in candidates.items():
        phases = {sale_phase(terms.get((org_id, cluster_id), []), now) for org_id in orgs}
        if ACTIVE not in phases and READ_ONLY in phases:
            read_only.add(cluster_id)
    return read_only


async def reaches_only_by_purchase(db: AsyncSession, email: Optional[str], cluster_id: str) -> bool:
    """Whether this account reaches the cluster through purchases and nothing else.

    Such an account may not bind the cluster to another organization: the binding it wrote would
    carry it as registrant and outlive the purchase (security review 2026-10-27).
    """
    if not email:
        return False
    _full, candidates = await _reach(db, email)
    return cluster_id in candidates


async def sold_to_without_registering(
    db: AsyncSession, organization_id: int, cluster_ids: Iterable[str], email: Optional[str],
) -> Set[str]:
    """Of these clusters, the ones the organization reaches only by purchase and the caller did not sell.

    A rule there would grant access the purchase does not pay for, so only the seller's side (a
    member of an organization holding a registered binding on the cluster, or its registrant) or an
    Administrator may write one.
    """
    from app.models.db_models import Organization, OrganizationCluster, OrganizationMember

    wanted = sorted(set(cluster_ids))
    if not wanted:
        return set()
    sale_only = set((await db.execute(
        select(OrganizationCluster.cluster_id)
        .join(Organization, Organization.id == OrganizationCluster.organization_id)
        .where(
            OrganizationCluster.organization_id == organization_id,
            OrganizationCluster.cluster_id.in_(wanted),
            OrganizationCluster.registered_by.is_(None),
            Organization.source == "orcahub",
        )
    )).scalars().all())
    if not sale_only or not email:
        return sale_only
    lower = email.lower()
    seller_side = set((await db.execute(
        select(OrganizationCluster.cluster_id)
        .join(Organization, Organization.id == OrganizationCluster.organization_id)
        .outerjoin(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .where(
            OrganizationCluster.cluster_id.in_(sorted(sale_only)),
            OrganizationCluster.registered_by.is_not(None),
            Organization.is_active == True,  # noqa: E712
            or_(
                func.lower(OrganizationMember.user_email) == lower,
                func.lower(Organization.created_by) == lower,
                func.lower(OrganizationCluster.registered_by) == lower,
            ),
        )
    )).scalars().all())
    return sale_only - seller_side
