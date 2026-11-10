"""Push member changes from Orcastra back to OrcaHub.

Calls go to OrcaHub's external API, authenticated with the organization's API key credentials.
"""

import json
import logging
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_key_expiry import classify_key
from app.core.safe_http import SafeHTTPError
from app.services.orcahub_callbacks import post_to_app
from app.models.db_models import Organization, OrganizationMember
from app.services.integration_callback_url import bound_callback_url

logger = logging.getLogger(__name__)

_ROLE_TO_ORCAHUB = {
    "admin": "partner",    # an admin managing the org is pushed as a partner
    "partner": "partner",
    "tenant": "tenant",
}


async def _get_orcahub_credentials(org: Organization, owner_keys: Sequence[str] = ()) -> Optional[Dict[str, Any]]:
    """OrcaHub callback URL and API key credentials for an org, or None.

    The org must have source='orcahub' and a stored api_key_id; the result holds app_url,
    api_key_id and api_key_secret. The primary owner key is tried first. When it cannot call back
    (ended, or no URL stored with its current secret), the other keys of the owner set
    (`owner_keys`, oldest first) are tried in turn, so an organization another owner still serves
    keeps its callbacks. Nothing is written.
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
    """Push member changes to OrcaHub. False when the org is not synced or the call failed."""
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
        _status, head = await post_to_app(
            callback_url,
            content=json.dumps({"members": payload_members}).encode(),
            headers={
                "Content-Type": "application/json",
                "X-API-Key-ID": creds["api_key_id"],
                "X-API-Key-Secret": creds["api_key_secret"],
            },
        )
        try:
            data = json.loads(head) if head else {}
        except ValueError:
            data = {}
        counts = data.get("data", {}) if isinstance(data, dict) else {}
        logger.info(
            f"Pushed {len(payload_members)} member(s) to OrcaHub: "
            f"synced={counts.get('synced', 0)}, removed={counts.get('removed', 0)}"
        )
        return True
    except SafeHTTPError as e:
        logger.error(f"OrcaHub member push failed: {e}")
    except Exception as e:
        logger.error(f"OrcaHub member push failed: {type(e).__name__}")

    return False


async def push_member_removal_to_orcahub(
    db: AsyncSession,
    user_email: str,
    org_ids: List[int],
) -> bool:
    """Tell OrcaHub a user left the given organizations, for those OrcaHub syncs with a key.

    important: only the organizations the removal left. A "remove" sent to every keyed OrcaHub
    organization would let a partner clearing a tenant from its own organization also drop that
    person from another partner's organization on the next sync.

    The callback carries no organization id, so OrcaHub reads the key; two removed organizations
    on one key get one call.
    """
    if not org_ids:
        return True
    result = await db.execute(
        select(Organization).where(
            and_(
                Organization.id.in_(sorted(set(org_ids))),
                Organization.source == "orcahub",
                Organization.api_key_id.isnot(None),
            )
        ).order_by(Organization.id)
    )
    orgs = result.scalars().all()

    # important: a key the person still sits under, in an organization this removal did not reach
    # (another partner's, or a synced partner row it kept), hears nothing, since OrcaHub would read
    # "remove" for the whole key.
    still_held = set((await db.execute(
        select(Organization.api_key_id)
        .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .where(
            func.lower(OrganizationMember.user_email) == (user_email or "").strip().lower(),
            Organization.api_key_id.in_([org.api_key_id for org in orgs]),
        )
    )).scalars().all())

    success = True
    pushed_keys: set = set(still_held)
    for org in orgs:
        if org.api_key_id in pushed_keys:
            continue
        pushed_keys.add(org.api_key_id)
        result = await push_members_to_orcahub(
            db,
            org.id,
            [{"email": user_email, "action": "remove"}],
        )
        if not result:
            success = False

    return success
