"""Which organization a buyer takes its branding from: the partner organization that sold to it.

Provisioning records a link, not a copy, so later branding changes reach the buyer. It is filled
only while `provider_link_source` is unset, so an Administrator's choice (with or without a
provider) and an earlier link both stand. The seller is the organization bound to the sold
cluster whose registrant is one of its members, the fact the proof of sale reads
(services/cluster_parties.py). If an Administrator registered the cluster, the key's minter
decides when it runs exactly one organization. Anything ambiguous is left unlinked and logged.

important: the link decides branding and nothing else. No access check reads it
(tests/test_the_provider_link_decides_branding_only.py).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import Organization, OrganizationCluster, OrganizationMember
from app.services.activity_logger import get_activity_logger

logger = logging.getLogger(__name__)


async def seller_of(db: AsyncSession, buyer_id: int, cluster_id: str, key_minter: Optional[str]) -> Optional[int]:
    registered = (await db.execute(
        select(OrganizationCluster.organization_id)
        .join(Organization, Organization.id == OrganizationCluster.organization_id)
        .join(OrganizationMember, and_(
            OrganizationMember.organization_id == OrganizationCluster.organization_id,
            func.lower(OrganizationMember.user_email) == func.lower(OrganizationCluster.registered_by),
        ))
        .where(
            OrganizationCluster.cluster_id == cluster_id,
            OrganizationCluster.registered_by.is_not(None),
            OrganizationCluster.organization_id != buyer_id,
            Organization.is_active == True,  # noqa: E712
        )
        .distinct()
    )).scalars().all()
    if len(registered) == 1:
        return registered[0]
    if registered:
        logger.info("Branding provider for organization %s left unset: cluster %s has %d registrants",
                    buyer_id, cluster_id, len(registered))
        return None
    if not key_minter:
        return None
    runs = (await db.execute(
        select(Organization.id)
        .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .where(
            func.lower(OrganizationMember.user_email) == key_minter.lower(),
            Organization.is_active == True,  # noqa: E712
            Organization.id != buyer_id,
            or_(
                OrganizationMember.role == "partner",
                and_(func.lower(Organization.created_by) == key_minter.lower(), Organization.source == "local"),
            ),
        )
        .distinct()
    )).scalars().all()
    return runs[0] if len(runs) == 1 else None


async def link_on_provision(
    db: AsyncSession, buyer: Organization, *, cluster_id: str, key_minter: Optional[str], api_key_id: str,
) -> Optional[int]:
    """Fill the buyer's provider when nothing decided it yet. Never raises: branding must not fail a sale."""
    if buyer.provider_link_source is not None:
        return buyer.provider_organization_id
    try:
        async with db.begin_nested():
            seller = await seller_of(db, buyer.id, cluster_id, key_minter)
            if seller is None:
                return None
            buyer.provider_organization_id = seller
            buyer.provider_link_source = "provision"
            buyer.provider_linked_at = datetime.now(timezone.utc)
            buyer.provider_linked_by = f"key:{api_key_id}"
            await db.flush()
    except Exception:  # noqa: BLE001 - a sale goes through without a branding link
        logger.warning("Could not link organization %s to its branding provider", buyer.id, exc_info=True)
        return None
    logger.info("Organization %s takes its branding from organization %s", buyer.id, seller)
    return seller


class ProviderInvalid(ValueError):
    pass


async def set_provider(
    db: AsyncSession, organization: Organization, provider_id: Optional[int], actor: str,
) -> Organization:
    """An Administrator's choice, which provisioning then leaves alone. None means no provider."""
    if provider_id is not None:
        if provider_id == organization.id:
            raise ProviderInvalid("An organization cannot take its branding from itself.")
        provider = (await db.execute(
            select(Organization).where(Organization.id == provider_id, Organization.is_active == True)  # noqa: E712
        )).scalar_one_or_none()
        if provider is None:
            raise ProviderInvalid("The provider organization does not exist or is not active.")
    before = organization.provider_organization_id
    organization.provider_organization_id = provider_id
    organization.provider_link_source = "admin"
    organization.provider_linked_at = datetime.now(timezone.utc)
    organization.provider_linked_by = actor
    await db.commit()
    await db.refresh(organization)
    get_activity_logger().log_activity(
        action="organization.branding.provider.set",
        target_type="organization",
        target_id=str(organization.id),
        target_organization_id=organization.id,
        target_organization_slug=organization.slug,
        before_state={"provider_organization_id": before},
        after_state={"provider_organization_id": provider_id},
    )
    return organization
