"""Which /integrations/api-keys edits an add-on credential refuses.

An add-on installation's credential is an ordinary integration key in Vault, so every
/integrations/api-keys route reaches it once the caller's clusters overlap its grant. The
installation owns that grant: install writes it under the operator's consent, rescope rewrites it,
rotation copies it onto the new key and the reconcile sweep narrows it. An edit through the key
routes changed what a third party holds outside that consent, and the next rescope withdrew it
again without anyone deciding so.

Per route, for a key carrying extension_install_id:
  PUT    /api-keys/{id}/capabilities    refused, 409. Rescope the installation.
  PUT    /api-keys/{id}/permissions     refused, 409. Rescope the installation.
  DELETE /api-keys/{id}                 refused, 409, while an installation claims the key as its
                                        api_key_id or previous_api_key_id. Rotation and rescope read
                                        the current key before they write, so deleting it broke both
                                        with a 503 that says the store is down, and the previous key
                                        is the add-on's working credential until its rotation grace
                                        ends. Revoke stops either now, and uninstall removes both
                                        with the signing secret and settings. A key no installation
                                        claims (superseded by a rotation without grace, rotated out
                                        twice, or reclaimed by the reconcile sweep) is deletable.
  POST   /api-keys/{id}/rotate-secret   refused, 409. The installation rotates its own credential
                                        (POST /extensions/installations/{id}/credential/rotate),
                                        which issues a new key with the same grant and retires this
                                        one, at once or after the grace window the operator chose.
  POST   /api-keys/{id}/revoke          allowed. The kill switch, for whoever may revoke today.
                                        Rescope keeps the revocation (it rewrites two fields) and
                                        rotation issues a working key again. Like any key's, it is
                                        refused while the key owns an active OrcaHub organization
                                        (services/integration_key_ownership.py), and the refusal
                                        names suspending the add-on as the way to stop it now.
  GET    /api-keys, /api-keys/{id}      allowed, reads.
  POST   /api-keys/{id}/users           allowed. A user mapping is only listed and counted, and
                                        nothing that authorizes a request reads it.
  GET    /api-keys/{id}/users           allowed, a read.
  DELETE /api-keys/{id}/users/{user}    allowed. Admin only, and it removes a mapping.
  POST   /api-keys                      not keyed. It cannot write extension_install_id.

important: each check runs after _authorize_api_key, so a key outside the caller's clusters still
answers 404 and the 409 never tells anyone an id they cannot see exists. On DELETE the ownership
check runs before the installation claim, so an add-on credential that owns organizations is told
how to move them first.
"""

import logging
from typing import Any, Mapping

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.extension_models import ExtensionInstallation

logger = logging.getLogger(__name__)

GRANT_EDIT_DETAIL = (
    "This key is an add-on credential. Its add-on installation owns the grant, so its "
    "capabilities and cluster permissions change only by rescoping that installation."
)
DELETE_DETAIL = (
    "This key is the credential an add-on installation is using, either its current key or the "
    "one a rotation is phasing out. Revoke it to stop it now, or uninstall the add-on to remove it."
)
ROTATE_DETAIL = (
    "This key is an add-on credential. Its installation owns it, so rotate it from its installation, "
    "which issues a new credential and retires this one."
)
CLAIM_UNKNOWN_DETAIL = (
    "This key is an add-on credential and its installation could not be checked, so it was not "
    "deleted. Try again, or revoke it to stop it now."
)


def is_add_on_credential(metadata: Mapping[str, Any]) -> bool:
    return metadata.get("extension_install_id") is not None


def refuse_grant_edit(metadata: Mapping[str, Any]) -> None:
    """409 for a capability or cluster permission edit of an add-on credential."""
    if is_add_on_credential(metadata):
        raise HTTPException(status_code=409, detail=GRANT_EDIT_DETAIL)


def refuse_secret_rotation(metadata: Mapping[str, Any]) -> None:
    """409 for an in-place secret rotation of an add-on credential."""
    if is_add_on_credential(metadata):
        raise HTTPException(status_code=409, detail=ROTATE_DETAIL)


async def is_current_add_on_credential(db: AsyncSession, key_id: str) -> bool:
    """Whether this add-on credential is still some installation's current key.

    important: only a current credential may first-claim an organization. A key a rotation replaced
    still authenticates for its grace window, and an organization it claimed then would stay on a key
    that expires; one rotated with no grace, rotated twice, or whose installation was deleted is not
    any installation's current key either.
    """
    row = (await db.execute(
        select(ExtensionInstallation.id).where(ExtensionInstallation.api_key_id == key_id).limit(1)
    )).first()
    return row is not None


async def installation_claims(db: AsyncSession, key_id: str) -> bool:
    """Whether an installation still uses this key, as its live credential or its rotation's previous one."""
    row = (await db.execute(
        select(ExtensionInstallation.id).where(or_(
            ExtensionInstallation.api_key_id == key_id,
            ExtensionInstallation.previous_api_key_id == key_id,
        )).limit(1)
    )).first()
    return row is not None


async def refuse_delete(db: AsyncSession, key_id: str, metadata: Mapping[str, Any]) -> None:
    """409 for a delete of an add-on credential an installation still claims. 503 when unknown."""
    if not is_add_on_credential(metadata):
        return
    try:
        claimed = await installation_claims(db, key_id)
    except Exception as exc:
        logger.warning("Could not read which installation claims %s: %s", key_id, type(exc).__name__)
        raise HTTPException(status_code=503, detail=CLAIM_UNKNOWN_DETAIL) from exc
    if claimed:
        raise HTTPException(status_code=409, detail=DELETE_DETAIL)
