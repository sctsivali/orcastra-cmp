"""
OrcaHub Sync Service

Handles pushing member/branding changes from Orcastra back to OrcaHub
via the external API endpoints authenticated with API key credentials.
"""

import logging
from typing import Any, Dict, List, Optional, Sequence

import httpx
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_key_expiry import classify_key
from app.models.db_models import Organization
from app.services.integration_callback_url import bound_callback_url

logger = logging.getLogger(__name__)

# orcastra role -> orcahub member role mapping
_ROLE_TO_ORCAHUB = {
    "admin": "partner",    # admin managing org -> push as partner
    "partner": "partner",
    "tenant": "tenant",
}


async def _get_orcahub_credentials(org: Organization, owner_keys: Sequence[str] = ()) -> Optional[Dict[str, Any]]:
    """
    Retrieve OrcaHub callback URL and API key credentials for an org.
    The org must have source='orcahub' and a stored api_key_id.
    Returns dict with app_url, api_key_id, api_key_secret or None.

    The primary owner key is asked first. When it cannot call back (ended, or no URL stored with its
    current secret), the other keys of the owner set (`owner_keys`, oldest first) are tried in turn, so
    an organization another owner still serves keeps its callbacks. Nothing is written.
    """
    if org.source != "orcahub" or not org.api_key_id:
        return None
    for key_id in [org.api_key_id, *[k for k in owner_keys if k and k != org.api_key_id]]:
        creds = _credentials_of(org, key_id)
        if creds:
            return creds
    return None


def _credentials_of(org: Organization, key_id: str) -> Optional[Dict[str, Any]]:
    try:
        from app.routers.integrations import (
            get_vault_integrations_client,
            VAULT_INTEGRATIONS_PATH,
        )

        vault_client = get_vault_integrations_client()
        metadata = vault_client.vault_read(
            f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/metadata"
        )
        # important: a revoked, inactive or expired key gets no callback. Its integration is
        # gone, and the raw secret a callback carries must not leave the process for it, so the
        # secret is not even read.
        if classify_key(metadata) != "active":
            logger.info(
                "OrcaHub callback skipped for api key %s: the key is revoked, inactive or expired",
                key_id,
            )
            return None
        secret_data = vault_client.vault_read(
            f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{key_id}/secret"
        )
        # important: only a URL the key's current secret stored, so a URL planted with a secret that
        # a rotation replaced never receives the new one. See services/integration_callback_url.py.
        app_url = bound_callback_url(metadata, secret_data.get("hash"))
        raw_secret = secret_data.get("raw")

        if not app_url:
            logger.warning(f"No callback URL stored with the current secret of API key {key_id}")
            return None

        if not raw_secret:
            logger.warning(f"No raw secret stored for API key {key_id}, cannot call back OrcaHub")
            return None

        return {
            "app_url": app_url,
            "api_key_id": key_id,
            "api_key_secret": raw_secret,
        }
    except Exception as e:
        logger.warning(f"Failed to retrieve OrcaHub credentials for org {org.id}: {e}")
        return None


async def push_members_to_orcahub(
    db: AsyncSession,
    org_id: int,
    members: List[Dict[str, Any]],
) -> bool:
    """
    Push member changes to OrcaHub.
    """
    # load org to get credentials
    result = await db.execute(select(Organization).where(Organization.id == org_id))
    org = result.scalar_one_or_none()
    if not org:
        logger.warning(f"Org {org_id} not found for OrcaHub push")
        return False

    from app.services.organization_owners import owner_key_ids

    creds = await _get_orcahub_credentials(org, await owner_key_ids(db, org.id))
    if not creds:
        logger.debug(f"Org {org_id} is not OrcaHub-synced or has no callback URL, skipping push")
        return False

    # map roles for OrcaHub
    payload_members = []
    for m in members:
        orcahub_role = _ROLE_TO_ORCAHUB.get(m.get("role", "tenant"), "tenant")
        payload_members.append({
            "email": m["email"],
            "name": m.get("name"),
            "role": orcahub_role,
            "action": m.get("action", "upsert"),
        })

    callback_url = f"{creds['app_url'].rstrip('/')}/api/external/members/sync"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                callback_url,
                headers={
                    "X-API-Key-ID": creds["api_key_id"],
                    "X-API-Key-Secret": creds["api_key_secret"],
                },
                json={"members": payload_members},
            )
            resp.raise_for_status()
            data = resp.json()
            logger.info(
                f"Pushed {len(payload_members)} member(s) to OrcaHub: "
                f"synced={data.get('data', {}).get('synced', 0)}, "
                f"removed={data.get('data', {}).get('removed', 0)}"
            )
            return True
    except httpx.HTTPStatusError as e:
        logger.error(f"OrcaHub member push failed: HTTP {e.response.status_code}")
    except Exception as e:
        logger.error(f"OrcaHub member push failed: {e}")

    return False


async def push_member_removal_to_orcahub(
    db: AsyncSession,
    user_email: str,
) -> bool:
    """
    Push removal of a user from ALL orgs to OrcaHub.
    Finds all OrcaHub-synced orgs this user was in and sends remove action.
    """
    # find all orcahub-sourced orgs
    result = await db.execute(
        select(Organization).where(
            and_(
                Organization.source == "orcahub",
                Organization.api_key_id.isnot(None),
            )
        )
    )
    orgs = result.scalars().all()

    success = True
    for org in orgs:
        result = await push_members_to_orcahub(
            db,
            org.id,
            [{"email": user_email, "action": "remove"}],
        )
        if not result:
            success = False

    return success
