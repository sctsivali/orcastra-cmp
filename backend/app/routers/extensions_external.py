"""What an add-on can ask about itself, using its own credential.

One endpoint, the reconcile backstop: an add-on that missed an event, or wants to know whether its
reach changed, polls it instead of assuming its install-time grant still holds.

important: exempt from a capability gate, like `/whoami`. Gating it on `inventory.read` would make
the call that diagnoses a credential unavailable to the credentials that need it, ex; an
installation whose only capability is `storage.upload`. It grants no access and reports only what
the presenting installation holds.
"""

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_key_expiry import days_remaining, expiry_state
from app.core.database import get_db
from app.core.extension_manifest import EXTENSION_EVENTS
from app.models.db_models import Organization
from app.models.extension_models import Extension, ExtensionInstallation
from app.routers.integrations import get_vault_integrations_client, validate_api_key
from app.services import extension_settings

logger = logging.getLogger(__name__)

external_router = APIRouter(prefix="/extensions", tags=["Extensions - External"])


@external_router.get(
    "/self",
    summary="Describe the presenting installation",
    description=(
        "What this credential is installed as, what it may reach, and its resolved settings. "
        "Poll this to converge after a missed event."
    ),
)
async def describe_self(
    api_key: Dict[str, Any] = Depends(validate_api_key),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Answers for the credential presented, and for nothing else.

    Every field is derived from the installation this key belongs to, so there is no id a caller
    could substitute to read somebody else's.
    """
    install = (
        await db.execute(
            select(ExtensionInstallation).where(
                ExtensionInstallation.api_key_id == api_key.get("id")
            )
        )
    ).scalar_one_or_none()

    if install is None:
        # A valid integration key that is not an installation. A 404 would read as "this
        # endpoint does not exist".
        return {
            "installed": False,
            "reason": "This credential is an integration key, not an add-on installation.",
        }

    extension = (
        await db.execute(select(Extension).where(Extension.id == install.extension_id))
    ).scalar_one()
    organization = (
        await db.execute(
            select(Organization).where(Organization.id == install.organization_id)
        )
    ).scalar_one_or_none()

    # The add-on reads its own settings including secrets, because it is the party that typed
    # them. The dashboard never gets this shape.
    resolved = extension_settings.read_resolved_settings(
        get_vault_integrations_client(), install.id, install.settings
    )

    return {
        "installed": True,
        "installation_id": install.id,
        "status": install.status,
        "extension": {
            "slug": extension.slug,
            "name": extension.name,
            "version": extension.version,
        },
        "organization": {
            "id": install.organization_id,
            "slug": organization.slug if organization else None,
            "name": organization.name if organization else None,
        },
        "granted_capabilities": list(install.granted_capabilities or []),
        "granted_scope": list(install.granted_scope or []),
        "settings": resolved,
        "credential_generation": install.credential_generation,
        # The credential's own expiry, so an add-on polling this endpoint sees a lapse coming
        # before it meets a 401. `never` is a valid state: a key with no expiry is supported.
        "credential": {
            "expires_at": api_key.get("expires_at"),
            "expires_in_days": days_remaining(api_key.get("expires_at")),
            "state": expiry_state(api_key.get("expires_at")),
        },
        "available_events": sorted(EXTENSION_EVENTS),
    }
