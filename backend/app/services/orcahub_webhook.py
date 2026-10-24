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

import httpx
from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.db_models import Organization, OrganizationCluster
from app.services.orcahub_sync import _get_orcahub_credentials

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
    body = json.dumps(payload, separators=(",", ":"))
    ts = str(int(time.time()))
    sig = hmac.new(creds["api_key_secret"].encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    url = f"{creds['app_url'].rstrip('/')}/api/webhooks/orcastra"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            url,
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-API-Key-ID": creds["api_key_id"],
                "X-Orcastra-Timestamp": ts,
                "X-Orcastra-Signature": sig,
            },
        )
        resp.raise_for_status()


async def emit_orcahub_event_durable(
    *,
    event_id: str,
    event_type: str,
    cluster_id: str,
    project: Optional[str],
    name: str,
    occurred_at: Optional[str] = None,
    state: Optional[str] = None,
    resource_type: str = "INSTANCE",
    namespace: str = "orcastra-migration",
) -> int:
    """Deliver one retry-safe OrcaHub event from a durable caller.

    The caller retains its outbox row until this returns. A per-organization UUID derived from
    the stable outbox id gives OrcaHub the same idempotency key after a partial delivery or worker
    crash. Endpoint and signing credentials still come only from the stored integration.
    """
    async with async_session_maker() as db:
        orgs = await _orcahub_orgs_for_cluster(db, cluster_id)
        owner_sets = await _owner_sets(db, orgs)
    if not orgs:
        return 0

    occurred = occurred_at or datetime.now(timezone.utc).isoformat()
    delivered = 0
    for org in orgs:
        creds = await _get_orcahub_credentials(org, owner_sets.get(org.id, []))
        if not creds:
            continue
        stable_event_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"{namespace}:{event_id}:{org.id}")
        )
        payload = {
            "eventId": stable_event_id,
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
        await _post_signed(creds, payload)
        delivered += 1
    return delivered


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
                    except Exception as e:
                        if attempt == 2:
                            logger.warning(f"OrcaHub webhook emit failed ({event_type} {name}): {e}")
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
