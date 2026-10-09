"""
OrcaHub Event Webhook (emitter)

Pushes resource lifecycle events from Orcastra to OrcaHub in real time so OrcaHub
can release quota / surface out-of-band resources instantly instead of waiting for
its periodic reconcile. Best-effort with bounded retry; OrcaHub's reconcile is the
durability backstop, so a dropped event self-heals.

Signed per-connection: reuses the API key secret OrcaHub already issued for this org
(stored in Vault). Signature = HMAC-SHA256(secret, "{timestamp}.{body}").
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select

from app.core.database import async_session_maker
from app.core.error_sanitizer import sanitize_error
from app.core.safe_http import guarded_request
from app.core.url_safety import UnsafeURLError
from app.models.db_models import Organization, OrganizationCluster
from app.services.orcahub_sync import (
    CALLBACK_RESPONSE_MAX_BYTES,
    _get_orcahub_credentials,
    callback_networks,
    callback_requires_https,
)

logger = logging.getLogger(__name__)


async def _orcahub_orgs_for_cluster(db, cluster_id: str):
    """OrcaHub-synced orgs bound to this cluster (usually exactly one: the partner)."""
    result = await db.execute(
        select(Organization)
        .join(OrganizationCluster, OrganizationCluster.organization_id == Organization.id)
        .where(
            OrganizationCluster.cluster_id == cluster_id,
            Organization.source == "orcahub",
            Organization.api_key_id.isnot(None),
        )
    )
    return result.scalars().unique().all()


async def _owner_sets(db, orgs) -> dict:
    """Each organization's owner keys, oldest first, for the callback fallback past a dead primary."""
    from app.services.organization_owners import owners_of

    sets = await owners_of(db, [org.id for org in orgs]) if orgs else {}
    return {org_id: [row.api_key_id for row in rows] for org_id, rows in sets.items()}


async def _post_signed(creds: dict, payload: dict) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode()
    ts = str(int(time.time()))
    sig = hmac.new(creds["api_key_secret"].encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    url = f"{creds['app_url'].rstrip('/')}/api/webhooks/orcastra"
    # note: raw bytes, not json=. OrcaHub verifies the signature over the exact body it receives.
    await guarded_request(
        "POST",
        url,
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-API-Key-ID": creds["api_key_id"],
            "X-Orcastra-Timestamp": ts,
            "X-Orcastra-Signature": sig,
        },
        max_bytes=CALLBACK_RESPONSE_MAX_BYTES,
        max_redirects=0,
        require_https=callback_requires_https(creds["app_url"]),
        allowed_networks=callback_networks(),
        raise_for_status=True,
        timeout=10.0,
    )


async def _emit(event_type: str, cluster_id: str, project: Optional[str], name: str, state: Optional[str], resource_type: str) -> None:
    try:
        async with async_session_maker() as db:
            orgs = await _orcahub_orgs_for_cluster(db, cluster_id)
            if not orgs:
                return
            owner_sets = await _owner_sets(db, orgs)
            occurred = datetime.now(timezone.utc).isoformat()
            for org in orgs:
                creds = await _get_orcahub_credentials(org, owner_sets.get(org.id, []))
                if not creds:
                    continue
                # Stable per-org event id (idempotency key is global per source+eventId).
                event_id = str(uuid.uuid4())
                payload = {
                    "eventId": event_id,
                    "eventType": event_type,
                    "occurredAt": occurred,
                    "orcastraClusterId": cluster_id,
                    "orcastraProjectName": project or "default",
                    "resourceType": resource_type,
                    "name": name,
                    "externalUpdatedAt": occurred,
                }
                if state:
                    payload["state"] = state
                for attempt in range(3):
                    try:
                        await _post_signed(creds, payload)
                        logger.info(f"Emitted {event_type} to OrcaHub for {name} (org {org.id})")
                        break
                    except UnsafeURLError as e:
                        # note: a refused URL stays refused, so retrying only costs the sleeps.
                        logger.warning(
                            "OrcaHub webhook refused for api key %s: %s", creds["api_key_id"], e
                        )
                        break
                    except Exception as e:
                        if attempt == 2:
                            logger.warning(
                                "OrcaHub webhook emit failed (%s %s): %s",
                                event_type, name, sanitize_error(str(e)),
                            )
                        else:
                            await asyncio.sleep(0.5 * (attempt + 1))
    except Exception as e:
        logger.warning(f"OrcaHub webhook emit error ({event_type} {name}): {e}")


def schedule_orcahub_event(
    event_type: str,
    cluster_id: str,
    project: Optional[str],
    name: str,
    state: Optional[str] = None,
    resource_type: str = "INSTANCE",
) -> None:
    """Fire-and-forget. Never blocks or raises into the caller."""
    try:
        asyncio.get_running_loop().create_task(_emit(event_type, cluster_id, project, name, state, resource_type))
    except RuntimeError:
        logger.debug("No running event loop for OrcaHub webhook emit; skipped")
