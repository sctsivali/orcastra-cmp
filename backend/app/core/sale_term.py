"""A purchase's paid term, applied to everything the purchase reaches, its own rules included.

Provisioning binds the seller's cluster to the buyer's OrcaHub organization with no registrant
(`organization_clusters.registered_by IS NULL`, and no `granted_by`): a sale-only binding. The buyer's reach there
follows that organization's OrcaHub policies naming the cluster: `active` while a live one's paid
term runs, `read_only` (retention, view only) once every live one is past it, `ended` with none.

core/tenant_term.py clamps the OrcaHub policies themselves. This module applies the same phase to
what the purchase reaches indirectly: a buyer founder promoted to role_partner (no tenant ACL),
and a local policy a founder wrote in the buyer organization. Registered bindings, local
organizations and the organization's creator are not sale-only and are untouched.
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
            OrganizationCluster.granted_by,
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
    for cluster_id, org_id, registered_by, granted_by, source, created_by in rows:
        # An administrator's grant is not a purchase, though it carries no registrant either.
        if (registered_by is not None or granted_by is not None or source != "orcahub"
                or (created_by or "").lower() == lower):
            full.add(cluster_id)
        else:
            sale_only.setdefault(cluster_id, set()).add(org_id)
    return full, {c: orgs for c, orgs in sale_only.items() if c not in full}


async def partner_purchase_reach(db: AsyncSession, email: Optional[str]) -> Tuple[Set[str], Set[str]]:
    """(clusters reached only by purchase, of those the ones past every purchase's paid term).

    For a role_partner account. On the first set it may not grant anything a purchase does not pay
    for (an integration key, an add-on, a binding); on the second it may look but not change
    anything, since during retention the buyer has view only.
    """
    if not email:
        return set(), set()
    _full, candidates = await _reach(db, email)
    if not candidates:
        return set(), set()
    return set(candidates), await _past_term(db, candidates)


async def partner_read_only_clusters(db: AsyncSession, email: Optional[str]) -> Set[str]:
    """Clusters this account reaches only through purchases that are all past their paid term."""
    return (await partner_purchase_reach(db, email))[1]


async def purchase_phases(db: AsyncSession, email: Optional[str]) -> Dict[str, str]:
    """The phase of every cluster this account reaches only by purchase (ACTIVE, READ_ONLY, ENDED).

    For what acts for the account without its session: a key it minted, a schedule it made.
    """
    if not email:
        return {}
    _full, candidates = await _reach(db, email)
    if not candidates:
        return {}
    return await _phases(db, candidates)


async def _phases(db: AsyncSession, candidates: Dict[str, Set[int]]) -> Dict[str, str]:
    # The account's best purchase on a cluster decides: one running term keeps it active.
    terms = await orcahub_terms(db, {o for orgs in candidates.values() for o in orgs})
    now = datetime.now(timezone.utc)
    out: Dict[str, str] = {}
    for cluster_id, orgs in candidates.items():
        phases = {sale_phase(terms.get((org_id, cluster_id), []), now) for org_id in orgs}
        out[cluster_id] = ACTIVE if ACTIVE in phases else READ_ONLY if READ_ONLY in phases else ENDED
    return out


async def _past_term(db: AsyncSession, candidates: Dict[str, Set[int]]) -> Set[str]:
    return {c for c, phase in (await _phases(db, candidates)).items() if phase == READ_ONLY}


async def purchase_only_clusters(db: AsyncSession, email: Optional[str]) -> Set[str]:
    """The clusters this account reaches through purchases and nothing else, whatever their term."""
    if not email:
        return set()
    _full, candidates = await _reach(db, email)
    return set(candidates)


async def reaches_only_by_purchase(db: AsyncSession, email: Optional[str], cluster_id: str) -> bool:
    """Whether this account reaches the cluster through purchases and nothing else.

    Such an account may not bind the cluster to another organization: the binding it wrote would
    carry it as registrant and outlive the purchase.
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
            OrganizationCluster.granted_by.is_(None),
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
