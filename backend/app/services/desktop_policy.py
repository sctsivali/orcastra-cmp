"""Whether an organization has consented to an agent seeing and driving its guests' desktops.

Two consents have to line up before a desktop action runs, and they are given by different people:

  the capability on the key   an integrator's operator decides what the integration may attempt
  this policy                 the organization owning the cluster decides whether it agrees at all

Keeping them separate matters because screen contents leave the deployment for a third-party
model. That is a disclosure to a sub-processor, not an ordinary API call, and it is not something
the person issuing an API key can decide on behalf of a tenant whose desktop it is.

important: an absent row reads exactly like a disabled one. An organization that has never been
asked has not agreed, so the default is refusal rather than inheritance.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import DesktopControlPolicy

logger = logging.getLogger(__name__)

__all__ = ["DesktopConsent", "get_consent", "consent_for_cluster", "set_consent"]


@dataclass(frozen=True)
class DesktopConsent:
    """What an organization currently permits."""

    enabled: bool = False
    allow_input: bool = False

    @property
    def may_view(self) -> bool:
        return self.enabled

    @property
    def may_input(self) -> bool:
        # note: input requires BOTH. `allow_input` alone would let a policy row that is disabled
        # overall still authorize typing, which reads as a bug the first time somebody turns
        # desktop control off and expects everything to stop.
        return self.enabled and self.allow_input


DENIED = DesktopConsent(enabled=False, allow_input=False)


async def get_consent(db: AsyncSession, organization_id: Optional[int]) -> DesktopConsent:
    """What this organization permits. Refusal when there is no organization or no row."""
    if organization_id is None:
        # A cluster with no organization bound has nobody who could have consented. Refusing is
        # the only honest answer, and it is also what stops an unbound cluster being the soft spot.
        return DENIED
    row = (
        await db.execute(
            select(DesktopControlPolicy).where(
                DesktopControlPolicy.organization_id == organization_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return DENIED
    return DesktopConsent(enabled=bool(row.enabled), allow_input=bool(row.allow_input))


async def consent_for_cluster(db: AsyncSession, cluster_id: str) -> tuple[DesktopConsent, Optional[str]]:
    """What EVERY organization sharing this cluster permits, and the first that does not.

    important: the intersection, not one organization's row. A cluster can be bound to more than
    one organization, and one on this deployment is: nuc2 carries both a provider org and a
    partner's. Asking only the lowest-id binding, which is what `resolve_cluster_org` answers and
    what this gate first shipped with, means one tenant's yes authorises capturing another
    tenant's screens. Neither tenant can tell whose guest is whose from the cluster alone, so the
    only honest rule is that all of them have to agree.

    Failing closed twice over: an unbound cluster has nobody who could have consented, and a
    single dissenting organization is enough to refuse.
    """
    from app.core.org_scope import resolve_cluster_orgs

    orgs = await resolve_cluster_orgs(cluster_id, db)
    if not orgs:
        return DENIED, None

    view = True
    inputs = True
    dissenting: Optional[str] = None
    for org_id, slug in orgs:
        consent = await get_consent(db, org_id)
        if not consent.may_view and dissenting is None:
            dissenting = slug
        view = view and consent.may_view
        inputs = inputs and consent.may_input
        if not inputs and dissenting is None and not consent.may_input:
            dissenting = slug
    return DesktopConsent(enabled=view, allow_input=view and inputs), dissenting


async def set_consent(
    db: AsyncSession,
    organization_id: int,
    *,
    enabled: bool,
    allow_input: bool,
    updated_by: str,
) -> DesktopConsent:
    """Record an organization's decision, creating the row on first use."""
    row = (
        await db.execute(
            select(DesktopControlPolicy).where(
                DesktopControlPolicy.organization_id == organization_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = DesktopControlPolicy(organization_id=organization_id)
        db.add(row)
    row.enabled = enabled
    # Turning the feature off clears the input consent rather than leaving it set, so that turning
    # it back on later does not silently restore a stronger permission than the operator expects.
    row.allow_input = allow_input and enabled
    row.updated_by = updated_by
    await db.commit()
    logger.info(
        "Desktop control consent for org %s set to enabled=%s allow_input=%s by %s",
        organization_id, row.enabled, row.allow_input, updated_by,
    )
    return DesktopConsent(enabled=bool(row.enabled), allow_input=bool(row.allow_input))
