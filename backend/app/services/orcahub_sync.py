"""
OrcaHub Sync Service

Handles pushing member/branding changes from Orcastra back to OrcaHub
via the external API endpoints authenticated with API key credentials.
"""

import ipaddress
import logging
import re
import time
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Literal, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.blocking import run_blocking
from app.core.config import get_settings
from app.core.error_sanitizer import sanitize_error
from app.core.safe_http import guarded_request
from app.core.url_safety import NEVER_ALLOWED, IPNetwork, UnsafeURLError, validate_url_field
from app.models.db_models import Organization
from app.services.integration_callback_url import bound_callback_url

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

# OrcaHub's own address check (zod's email pattern), so what is sent is what it accepts. Pydantic's
# EmailStr is stricter and would drop addresses on special-use domains that OrcaHub takes.
_ORCAHUB_EMAIL = re.compile(
    r"^(?!\.)(?!.*\.\.)([A-Za-z0-9_'+\-\.]*)[A-Za-z0-9_+-]@([A-Za-z0-9][A-Za-z0-9\-]*\.)+[A-Za-z]{2,}$"
)
HOME_ORG_RESPONSE_MAX_BYTES = 1024 * 1024
HOME_ORG_TTL_SECONDS = 600.0
# note: per worker and on the event loop only. A miss costs one extra lookup, never a wrong push.
_home_org_cache: Dict[str, Tuple[float, str]] = {}


class OrcaHubMemberEntry(BaseModel):
    """One entry of OrcaHub's POST /api/external/members/sync body.

    important: OrcaHub rejects the whole batch with 400 on action "upsert" or a null name, which
    is what every push sent before, so none of them ever landed. `name` is present only as a
    string and `role` only on a sync, via model_dump(exclude_none=True).
    """

    model_config = ConfigDict(extra="forbid")

    email: str = Field(..., max_length=255)
    name: Optional[str] = Field(None, max_length=255)
    role: Optional[Literal["partner", "tenant"]] = None
    action: Literal["sync", "remove"]

    @field_validator("email")
    @classmethod
    def _an_address_orcahub_accepts(cls, value: str) -> str:
        if not _ORCAHUB_EMAIL.match(value):
            raise ValueError("not an email address OrcaHub accepts")
        return value


def orcahub_member_entries(members: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Dashboard member changes in OrcaHub's contract. An entry it would refuse is dropped."""
    entries: List[Dict[str, Any]] = []
    for member in members:
        remove = member.get("action") == "remove"
        name = member.get("name")
        try:
            entry = OrcaHubMemberEntry(
                email=str(member.get("email") or ""),
                name=name if isinstance(name, str) and name else None,
                role=None if remove else _ROLE_TO_ORCAHUB.get(member.get("role") or "tenant", "tenant"),
                action="remove" if remove else "sync",
            )
        except ValidationError:
            logger.warning("Skipping a member OrcaHub would refuse in its members/sync contract")
            continue
        entries.append(entry.model_dump(exclude_none=True))
    return entries


async def _connection_home_org_id(creds: Dict[str, Any]) -> Optional[str]:
    """The OrcaHub organization this connection belongs to, or None when it cannot be told.

    important: OrcaHub applies every members/sync entry to the connection's own organization,
    not to the Orcastra org it came from. Pushing for a buyer org the same key owns would add or
    remove the user in the seller org, and a removal there would undo a purchase membership.
    """
    key_id = creds["api_key_id"]
    now = time.monotonic()
    cached = _home_org_cache.get(key_id)
    if cached and now - cached[0] < HOME_ORG_TTL_SECONDS:
        return cached[1]
    url = f"{creds['app_url'].rstrip('/')}/api/external/organizations"
    try:
        resp = await guarded_request(
            "GET",
            url,
            headers={"X-API-Key-ID": key_id, "X-API-Key-Secret": creds["api_key_secret"]},
            max_bytes=HOME_ORG_RESPONSE_MAX_BYTES,
            max_redirects=0,
            require_https=callback_requires_https(creds["app_url"]),
            allowed_networks=callback_networks(),
            content_type_prefix="application/json",
            raise_for_status=True,
            timeout=10.0,
        )
        home = resp.json()["data"]["organizations"][0]["id"]
    except UnsafeURLError as exc:
        logger.warning("OrcaHub home organization lookup refused for api key %s: %s", key_id, exc)
        return None
    except Exception as exc:
        logger.warning(
            "OrcaHub home organization lookup failed for api key %s: %s", key_id, sanitize_error(str(exc))
        )
        return None
    if not isinstance(home, (str, int)) or isinstance(home, bool) or str(home) == "":
        return None
    _home_org_cache[key_id] = (now, str(home))
    return str(home)


def _read_orcahub_credentials_blocking(api_key_id: str) -> Optional[Dict[str, Any]]:
    """Both Vault reads a callback needs. Runs on the auth pool, never on the event loop.

    A revoked, inactive or expired key gets no callbacks: its integration is gone, and the raw
    secret it would carry should not leave the process for it.
    """
    from app.routers.integrations import (
        VAULT_INTEGRATIONS_PATH,
        _api_key_is_unusable,
        get_vault_integrations_client,
    )

    vault_client = get_vault_integrations_client()
    metadata = vault_client.vault_read(
        f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{api_key_id}/metadata"
    ) or {}
    if _api_key_is_unusable(metadata):
        logger.info("OrcaHub callback skipped for api key %s: the key is revoked, inactive or expired", api_key_id)
        return None
    secret_data = vault_client.vault_read(
        f"secret/data/{VAULT_INTEGRATIONS_PATH}/api_keys/{api_key_id}/secret"
    ) or {}
    # important: only a URL the key's current secret stored, so a URL planted with a secret that a
    # rotation replaced never receives the new one. See services/integration_callback_url.py.
    app_url = bound_callback_url(metadata, secret_data.get("hash"))
    if not app_url:
        logger.warning(f"No callback URL stored with the current secret of API key {api_key_id}")
        return None
    raw_secret = secret_data.get("raw")
    if not raw_secret:
        logger.warning(f"No raw secret stored for API key {api_key_id}, cannot call back OrcaHub")
        return None
    return {
        "app_url": app_url,
        "api_key_id": api_key_id,
        "api_key_secret": raw_secret,
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

    from app.services.lxd.proxy import VALIDATE_KEY_TIMEOUT, auth_executor

    for key_id in [org.api_key_id, *[k for k in owner_keys if k and k != org.api_key_id]]:
        try:
            creds = await run_blocking(
                _read_orcahub_credentials_blocking,
                key_id,
                timeout=VALIDATE_KEY_TIMEOUT,
                label=f"orcahub_credentials[{key_id}]",
                executor=auth_executor,
            )
        except Exception as e:
            logger.warning(
                "Failed to retrieve OrcaHub credentials for org %s: %s", org.id, sanitize_error(str(e))
            )
            continue
        if creds:
            return creds
    return None


async def push_members_to_orcahub(
    db: AsyncSession,
    org_id: int,
    members: List[Dict[str, Any]],
) -> bool:
    """
    Push member changes in an OrcaHub connection's own organization to OrcaHub.

    Only the connection's home organization is pushed (see _connection_home_org_id), and only
    while ORCAHUB_MEMBER_PUSH_ENABLED is on.
    """
    if not get_settings().orcahub_member_push_enabled:
        logger.debug("OrcaHub member push is disabled, skipping org %s", org_id)
        return False

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

    home = await _connection_home_org_id(creds)
    if home is None or home != str(org.external_id):
        logger.info(
            "OrcaHub member push skipped for organization %s: not the connection's own organization",
            org.slug,
        )
        return False

    payload_members = orcahub_member_entries(members)
    if not payload_members:
        return False

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
    organization_ids: Iterable[int],
) -> bool:
    """
    Tell OrcaHub about a removal, only for the organizations the user actually left.

    important: this used to select every orcahub organization and post the email to each one's
    callback, so every integration, including one whose app_url points at a planted endpoint,
    learned who was removed anywhere. The caller passes the ids its own delete returned.
    """
    ids = sorted({int(org_id) for org_id in organization_ids})
    if not ids:
        return True
    result = await db.execute(
        select(Organization).where(
            and_(
                Organization.id.in_(ids),
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
