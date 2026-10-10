"""Who may hand an add-on's organizations to a new secret, and the installation row lock that decides it.

Split from extension_install: rotation and uninstall decide on the installation row read again under
lock_installation, and a rotation, which carries what the old credential owns, is refused to anyone who
does not run the installation's organization (RotationNeedsAPartner).
"""

import logging
from typing import List

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import Organization
from app.models.extension_models import ExtensionInstallation
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
    """The installation row, locked and read again, for a decision about its credentials.

    important: rotation and uninstall decide on this row, never on the copy _load read before. Two
    rotations, or a rotation and an uninstall, each read the same api_key_id before either wrote, so the
    second carried or checked a key the first had already retired, and organizations were left on a
    credential no installation claims.
    note: FOR NO KEY UPDATE, so an organization delete's foreign-key check (FOR KEY SHARE) is not held up
    behind a rotation's Vault calls; the rotation's UPDATE and the uninstall's DELETE take the full lock.
    note: lock order is this row, then keys (sorted), then organizations (sorted, organization_owners);
    nothing locks an installation row while holding a key or organization lock. Call it before changing
    the object: populate_existing overwrites the session's copy.
    """
    locked = (await db.execute(
        select(ExtensionInstallation)
        .where(ExtensionInstallation.id == install.id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if locked is None:
        raise HTTPException(status_code=404, detail="Installation not found")
    return locked


async def owned_org_ids_or_503(db: AsyncSession, key_id: str) -> List[int]:
    try:
        return await owned_org_ids(db, key_id)
    except Exception as exc:  # noqa: BLE001 - unknown ownership refuses, it never allows
        logger.warning("Could not read which organizations api key %s owns: %s", key_id, type(exc).__name__)
        raise HTTPException(status_code=503, detail=OWNERSHIP_UNKNOWN_DETAIL) from None


async def runs_installation_organization(db: AsyncSession, organization_id: int, email: str) -> bool:
    """Whether this user runs the installation's organization: a partner row, or its creator if made here.

    note: a creator is stored with a "member" row (OrganizationService.create_organization), so it is
    counted through created_by, but only for an organization made in this dashboard: an OrcaHub one's
    created_by is the minter of the key that provisioned it (cluster_parties.home_partner_keys).
    """
    from app.models.db_models import OrganizationMember
    from app.services.cluster_parties import RUNNING_ROLES

    wanted = (email or "").strip().lower()
    if not wanted:
        return False
    partner = await db.execute(
        select(OrganizationMember.id).where(
            OrganizationMember.organization_id == organization_id,
            func.lower(OrganizationMember.user_email) == wanted,
            OrganizationMember.role.in_(RUNNING_ROLES),
        )
    )
    if partner.first() is not None:
        return True
    creator = await db.execute(
        select(Organization.id).where(
            Organization.id == organization_id,
            Organization.source == "local",
            func.lower(Organization.created_by) == wanted,
        )
    )
    return creator.first() is not None
