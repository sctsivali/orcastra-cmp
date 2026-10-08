"""Withdrawing tenant.provision from a key that owns organizations is asked about, not refused.

Owner decision, 2026-10-08. An owner key without tenant.provision still syncs its organizations and
their members, but provisioning buyer dashboard access for them stops: an order completed in
OrcaHub no longer gives its buyer access here, and no other key may provision for an organization
another key owns. An operator may choose that, unlike withdrawing org.sync or inventory.read, which
freezes the organizations and stays refused (integration_key_ownership.SYNC_CAPABILITIES). So the
capability edit answers 409 naming the consequence and the organizations, and the same request with
force goes through and is recorded as forced. There is no restriction beyond who may edit the key.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.integration_key_ownership import (
    MAX_LISTED_SLUGS,
    OWNERSHIP_UNKNOWN_DETAIL,
    name_organizations,
    owned_organizations,
    visible_organization_ids,
)

logger = logging.getLogger(__name__)

PROVISIONING_CAPABILITY = "tenant.provision"
PROVISIONING_WOULD_STOP = "provisioning_would_stop"
CONSEQUENCE = (
    "Provisioning buyer dashboard access for these organizations stops. Their sync and member updates "
    "continue."
)


def _message(total: int, slugs: List[str], hidden: int) -> str:
    one = total == 1
    them, their = ("it", "Its") if one else ("them", "Their")
    noun = "organization" if one else "organizations"
    return (
        f"This key owns {total} OrcaHub {noun} ({name_organizations(slugs, hidden)}). Withdrawing its "
        f"{PROVISIONING_CAPABILITY} capability stops provisioning buyer dashboard access for {them}: an "
        f"order completed in OrcaHub no longer gives its buyer access here. {their} sync and member "
        "updates continue. Confirm to withdraw it anyway."
    )


async def confirm_provisioning_withdrawal(
    db: AsyncSession, key_id: str, *, user: Any, force: bool
) -> Optional[Dict[str, Any]]:
    """None when the key owns no active OrcaHub organization. Otherwise 409 without force, and with
    it the fields the edit's audit record carries. 503 when what the key owns cannot be read.

    note: names only the organizations the caller may see and counts the rest, in the answer and in
    the record alike, as a refusal does: the caller can read the record.
    """
    try:
        owned = await owned_organizations(db, key_id)
        visible = await visible_organization_ids(db, user) if owned else None
    except Exception as exc:  # noqa: BLE001 - unknown ownership asks again, it never allows
        logger.warning("Could not read which organizations api key %s owns: %s", key_id, type(exc).__name__)
        raise HTTPException(status_code=503, detail=OWNERSHIP_UNKNOWN_DETAIL) from None
    if not owned:
        return None
    shown = [org for org in owned if visible is None or org.id in visible]
    hidden = len(owned) - len(shown)
    if force:
        return {
            "forced": True,
            "owned_organization_ids": [org.id for org in shown][:MAX_LISTED_SLUGS],
            "owned_organization_count": len(owned),
            "owned_organization_hidden_count": hidden,
        }
    slugs = [org.slug for org in shown]
    raise HTTPException(status_code=409, detail={
        "error": PROVISIONING_WOULD_STOP,
        "action": "capabilities",
        "capability": PROVISIONING_CAPABILITY,
        "message": _message(len(owned), slugs, hidden),
        "consequence": CONSEQUENCE,
        "api_key_id": key_id,
        "organization_count": len(owned),
        "organization_slugs": slugs[:MAX_LISTED_SLUGS],
        "hidden_organization_count": hidden,
        "proceed": "Send the same request with force set to true to withdraw it anyway.",
    })
