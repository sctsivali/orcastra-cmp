"""The installation row lock that an add-on credential's rotation and uninstall decide on.

Split from extension_install: rotation and uninstall decide on the installation row read again under
lock_installation, never on the copy the route loaded before.
"""

import logging
from typing import List

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.extension_models import ExtensionInstallation
from app.services.integration_key_ownership import OWNERSHIP_UNKNOWN_DETAIL
from app.services.organization_owners import owned_org_ids

logger = logging.getLogger(__name__)


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
