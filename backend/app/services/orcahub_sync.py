"""
OrcaHub Sync Service

Handles pushing member/branding changes from Orcastra back to OrcaHub
via the external API endpoints authenticated with API key credentials.
"""

import ipaddress
import logging
from functools import lru_cache
from typing import Optional, List, Dict, Any, Tuple
from urllib.parse import urlsplit

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.error_sanitizer import sanitize_error
from app.core.safe_http import guarded_request
from app.core.url_safety import NEVER_ALLOWED, IPNetwork, UnsafeURLError, validate_url_field
from app.models.db_models import Organization

logger = logging.getLogger(__name__)

CALLBACK_RESPONSE_MAX_BYTES = 64 * 1024


@lru_cache(maxsize=4)
def _parse_networks(raw: str) -> Tuple[IPNetwork, ...]:
    nets: List[IPNetwork] = []
    for part in (p.strip() for p in raw.split(",")):
        if not part:
            continue
        try:
            net = ipaddress.ip_network(part, strict=False)
        except ValueError:
            logger.error("Ignoring ORCAHUB_CALLBACK_ALLOWED_NETWORKS entry %r: not a network", part)
            continue
        if net.prefixlen == 0 or any(a in net for a in NEVER_ALLOWED if a.version == net.version):
            logger.error("Ignoring ORCAHUB_CALLBACK_ALLOWED_NETWORKS entry %r: too broad", part)
            continue
        nets.append(net)
    return tuple(nets)


def callback_networks() -> Tuple[IPNetwork, ...]:
    """Operator-allowed non-public networks. A bad entry narrows the policy, never widens it."""
    return _parse_networks(get_settings().orcahub_callback_allowed_networks or "")


def callback_requires_https(url: str) -> bool:
    """https everywhere, except a literal address inside an allowed network (ex; a tailnet)."""
    try:
        host = urlsplit(url).hostname or ""
        addr = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not any(addr.version == net.version and addr in net for net in callback_networks())


def check_callback_url(value: str) -> str:
    """Boundary check for a callback base URL. Pure, no DNS.

    note: every caller appends a fixed path to this value, and a '?' or '#' anywhere would turn
    that path into a query or a fragment of a URL the caller chose.
    """
    if "?" in value or "#" in value:
        raise ValueError("app_url must be a base URL without a query or fragment")
    return validate_url_field(
        value,
        field="app_url",
        require_https=callback_requires_https(value),
        allowed_networks=callback_networks(),
    )

# orcastra role -> orcahub member role mapping
_ROLE_TO_ORCAHUB = {
    "admin": "partner",    # admin managing org -> push as partner
    "partner": "partner",
    "tenant": "tenant",
}


async def _get_orcahub_credentials(org: Organization) -> Optional[Dict[str, Any]]:
    """
    Retrieve OrcaHub callback URL and API key credentials for an org.
    The org must have source='orcahub' and a stored api_key_id.
    Returns dict with app_url, api_key_id, api_key_secret_hash or None.
    """
    if org.source != "orcahub" or not org.api_key_id:
        return None

    try:
        from app.routers.integrations import (
            get_vault_integrations_client,
            VAULT_INTEGRATIONS_PATH,
        )

        vault_client = get_vault_integrations_client()
        metadata = vault_client.vault_read(
            f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{org.api_key_id}/metadata"
        )
        secret_data = vault_client.vault_read(
            f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{org.api_key_id}/secret"
        )
        app_url = metadata.get("app_url")
        raw_secret = secret_data.get("raw")

        if not app_url:
            logger.warning(f"No app_url stored for API key {org.api_key_id}")
            return None

        if not raw_secret:
            logger.warning(f"No raw secret stored for API key {org.api_key_id} — cannot call back OrcaHub")
            return None

        return {
            "app_url": app_url,
            "api_key_id": org.api_key_id,
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

    creds = await _get_orcahub_credentials(org)
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
    # important: app_url was chosen by whoever holds the key and this request carries the raw
    # secret, so it goes through the same URL policy as every other OrcaHub callback, re-checked
    # and pinned at the moment of the request.
    try:
        resp = await guarded_request(
            "POST",
            callback_url,
            headers={
                "X-API-Key-ID": creds["api_key_id"],
                "X-API-Key-Secret": creds["api_key_secret"],
            },
            json={"members": payload_members},
            max_bytes=CALLBACK_RESPONSE_MAX_BYTES,
            max_redirects=0,
            require_https=callback_requires_https(creds["app_url"]),
            allowed_networks=callback_networks(),
            raise_for_status=False,
            timeout=15.0,
        )
    except UnsafeURLError as exc:
        logger.warning("OrcaHub member push refused for api key %s: %s", creds["api_key_id"], exc)
        return False
    except Exception as exc:
        logger.error("OrcaHub member push failed: %s", sanitize_error(str(exc)))
        return False
    if not 200 <= resp.status_code < 300:
        logger.error("OrcaHub member push failed: HTTP %s", resp.status_code)
        return False
    try:
        data = resp.json()
    except ValueError:
        data = {}
    counts = data.get("data") if isinstance(data, dict) else None
    counts = counts if isinstance(counts, dict) else {}
    logger.info(
        "Pushed %d member(s) to OrcaHub: synced=%s, removed=%s",
        len(payload_members), counts.get("synced", 0), counts.get("removed", 0),
    )
    return True


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
