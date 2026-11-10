"""Who may hand an add-on's organizations to a new secret, and the installation row lock that decides it.

Rotation, uninstall, rescope and the reconcile sweep decide on the installation row read again
under lock_installation. A rotation, which carries what the previous credential owns, is refused to
anyone who does not run the installation's organization (RotationNeedsAPartner).
"""

import logging
from datetime import datetime, timezone
from typing import Any, List, Optional

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import Organization
from app.models.extension_models import ExtensionInstallation
from app.core.api_key_expiry import now_utc
from app.services.integration_key_ownership import OWNERSHIP_UNKNOWN_DETAIL
from app.services.organization_owners import owned_org_ids

logger = logging.getLogger(__name__)


class RotationNeedsAPartner(HTTPException):
    """403: a rotation would hand the add-on's organizations to someone who does not run its installation."""

    def __init__(self) -> None:
        super().__init__(status_code=403, detail={
            "error": "not_a_partner_of_the_installation_organization",
            "message": (
                "This add-on's credential owns OrcaHub organizations, and a rotation hands them to the new "
                "secret. Only an Orcastra administrator or a partner of the installation's organization can "
                "rotate it."
            ),
        })


async def lock_installation(db: AsyncSession, install: ExtensionInstallation) -> ExtensionInstallation:
    """lock_installation_id for an installation already loaded (see there)."""
    return await lock_installation_id(db, install.id)


async def lock_installation_id(db: AsyncSession, install_id: int) -> ExtensionInstallation:
    """The installation row, locked and read again, for a decision about its credentials.

    important: rotation, uninstall, rescope and the sweep decide on this row, never on a copy read before.
    Otherwise two rotations, or a rotation and an uninstall, read the same api_key_id before either writes,
    the second carries or checks a key the first already retired, and organizations are left on a
    credential no installation claims.

    FOR NO KEY UPDATE, so an organization delete's foreign-key check (FOR KEY SHARE) is not held up
    behind a rotation's Vault calls; the rotation's UPDATE and the uninstall's DELETE take the full lock.

    important: lock order is this row, then keys (sorted), then organizations (sorted, organization_owners);
    nothing locks an installation row while holding a key or organization lock. Call it before changing
    the object: populate_existing overwrites the session's copy.
    """
    locked = (await db.execute(
        select(ExtensionInstallation)
        .where(ExtensionInstallation.id == install_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if locked is None:
        raise HTTPException(status_code=404, detail="Installation not found")
    return locked


def live_credentials(install: Any, now: Optional[datetime] = None) -> List[str]:
    """The installation's credentials that still authenticate, sorted: its current key, and the previous
    one while its grace window lasts. A grant change or a revocation must reach every one of them."""
    keys = {install.api_key_id}
    previous, until = getattr(install, "previous_api_key_id", None), getattr(install, "previous_key_expires_at", None)
    if previous and until is not None:
        until = until if until.tzinfo else until.replace(tzinfo=timezone.utc)
        if until > (now or now_utc()):
            keys.add(previous)
    return sorted(k for k in keys if k)


async def owned_org_ids_or_503(db: AsyncSession, key_id: str) -> List[int]:
    try:
        return await owned_org_ids(db, key_id)
    except Exception as exc:  # noqa: BLE001 - unknown ownership refuses, it never allows
        logger.warning("Could not read which organizations api key %s owns: %s", key_id, type(exc).__name__)
        raise HTTPException(status_code=503, detail=OWNERSHIP_UNKNOWN_DETAIL) from None


def _runs(email: str):
    """The organizations a user runs: a partner row there, or its creator when it was made in this dashboard.

    A creator is stored with a "member" row (OrganizationService.create_organization), so it is
    counted through created_by, but only for a local organization: an OrcaHub one's created_by is the
    minter of the key that provisioned it (cluster_parties.home_partner_keys), a seller and not its
    runner. A "member" or "tenant" row is a buyer's or a teammate's, and an "admin" row is one a
    demotion left behind (cluster_parties.RUNNING_ROLES).
    """
    from app.models.db_models import OrganizationMember
    from app.services.cluster_parties import RUNNING_ROLES

    partner = (
        select(OrganizationMember.id)
        .where(
            OrganizationMember.organization_id == Organization.id,
            func.lower(OrganizationMember.user_email) == email,
            OrganizationMember.role.in_(RUNNING_ROLES),
        )
        .exists()
    )
    return or_(partner, and_(Organization.source == "local", func.lower(Organization.created_by) == email))


async def runs_installation_organization(db: AsyncSession, organization_id: int, email: str) -> bool:
    """Whether this user runs the installation's organization (_runs)."""
    wanted = (email or "").strip().lower()
    if not wanted:
        return False
    row = await db.execute(
        select(Organization.id).where(Organization.id == organization_id, _runs(wanted)).limit(1)
    )
    return row.first() is not None


async def orgs_the_user_runs(db: AsyncSession, email: str) -> List[int]:
    """The active organizations this user runs (_runs): the only ones whose add-ons it may install,
    read or manage, unless it is an Administrator."""
    wanted = (email or "").strip().lower()
    if not wanted:
        return []
    rows = await db.execute(
        select(Organization.id)
        .where(Organization.is_active == True, _runs(wanted))  # noqa: E712 - SQLAlchemy column comparison
        .order_by(Organization.id)
    )
    return [row[0] for row in rows.all()]
