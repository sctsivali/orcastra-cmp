"""Claiming, delivering and retiring one delivery at a time.

Three properties this file exists to hold.

Deliveries are claimed with `FOR UPDATE SKIP LOCKED`, not by electing a leader. The work is many
independent rows, so every worker pulling from the queue is the only source of delivery
parallelism and the queue degrades gracefully when one dies. Leader election would be worse than
nothing here: the lease this codebase already uses fails open on purpose, so during a Redis outage
all four workers would believe they lead and every event would go out four times.

Every delivery is re-authorised immediately before it is sent, not merely re-sent. The stored
grant is a record of consent, and an organization can lose a cluster between the moment an event
was enqueued and the moment it goes out. Without this, revocation is not real, which is the
property the whole installation model is sold on.

Retries use full jitter rather than plain exponential backoff. The failure mode is not one slow
receiver, it is a receiver that was down for an hour and comes back to a synchronised backlog;
full jitter is the variant that actually spreads that.
"""

import asyncio
import logging
import random
import socket
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_key_expiry import now_utc
from app.core.safe_http import SafeHTTPError, post_pinned
from app.core.url_safety import describe_url_risk
from app.models.extension_models import (
    ExtensionInstallation,
    WebhookDelivery,
    WebhookSubscription,
)
from app.services.extension_credentials import read_signing_secrets
from app.services.webhooks.envelope import ResourceEvent, assert_payload_is_clean, build_envelope
from app.services.webhooks.signing import (
    DELIVERY_ID_HEADER,
    EVENT_ID_HEADER,
    EVENT_TYPE_HEADER,
    TIMESTAMP_HEADER,
    SIGNATURE_HEADER,
    signature_header,
)

logger = logging.getLogger(__name__)

# Five attempts over roughly fifty minutes. Deliberately not the multi-hour schedule a general
# queue would use: a receiver down for longer than this reconciles through /extensions/self, and
# a schedule that cannot be exercised inside a test is a schedule nobody has seen work.
RETRY_SCHEDULE_SECONDS = (10, 60, 300, 900, 1800)
MAX_ATTEMPTS = len(RETRY_SCHEDULE_SECONDS)

# Enough consecutive failures that the endpoint is gone rather than flapping.
AUTO_SUSPEND_AFTER = 20

# What a receiver says is diagnostic, not payload.
MAX_RESPONSE_BYTES = 64 * 1024

# A worker that dies holding a row must not hold it forever.
STUCK_AFTER_SECONDS = 120

WORKER_ID = f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"


def _deployment_id() -> str:
    from app.core.config import get_settings

    base = (get_settings().public_api_base_url or "").strip()
    return base or "orcastra"


def _next_attempt_delay(attempt: int) -> float:
    """Full jitter over the scheduled backoff.

    `random.uniform(0, base)` rather than `base`, because the thundering herd this guards against
    is a queue resuming all at once, and only full jitter spreads a synchronised backlog.
    """
    base = RETRY_SCHEDULE_SECONDS[min(attempt, MAX_ATTEMPTS - 1)]
    return random.uniform(0, base)


# ── enqueue ────────────────────────────────────────────────────────────────────────────────────

async def enqueue_event(
    db: AsyncSession,
    event: ResourceEvent,
    *,
    subscriptions: Sequence[WebhookSubscription],
) -> List[WebhookDelivery]:
    """Create one row per interested subscription, in the caller's transaction.

    important: in the caller's transaction on purpose. The emitter this replaces fired a detached
    task, so "the database commit succeeded but the event vanished" was reachable and invisible.
    Enqueuing alongside the action means an event exists exactly when the thing it describes does.
    """
    created: List[WebhookDelivery] = []
    for subscription in subscriptions:
        if subscription.status != "active":
            continue
        if event.event_type not in (subscription.event_types or []):
            continue

        payload = build_envelope(
            event,
            deployment_id=_deployment_id(),
            subscription_id=subscription.id,
            installation_id=subscription.installation_id,
            sequence=int(time.time() * 1000),
        )
        # Structural, on the row rather than only in a test, so a future builder cannot widen it.
        assert_payload_is_clean(payload)

        delivery = WebhookDelivery(
            subscription_id=subscription.id,
            event_id=event.event_id,
            event_type=event.event_type,
            payload=payload,
            state="pending",
            next_attempt_at=now_utc(),
        )
        db.add(delivery)
        created.append(delivery)
    return created


# ── claiming ───────────────────────────────────────────────────────────────────────────────────

CLAIM_SQL = text(
    """
    UPDATE extension_webhook_deliveries
       SET state = 'inflight', locked_by = :worker, locked_at = now(), updated_at = now()
     WHERE id IN (
         SELECT id FROM extension_webhook_deliveries
          WHERE state = 'pending' AND next_attempt_at <= now()
          ORDER BY next_attempt_at
          FOR UPDATE SKIP LOCKED
          LIMIT :batch
     )
    RETURNING id
    """
)

REAP_SQL = text(
    """
    UPDATE extension_webhook_deliveries
       SET state = 'pending', locked_by = NULL, locked_at = NULL, updated_at = now()
     WHERE state = 'inflight'
       AND locked_at < now() - make_interval(secs => :stuck)
    RETURNING id
    """
)


async def claim_batch(db: AsyncSession, *, batch: int = 16) -> List[int]:
    result = await db.execute(CLAIM_SQL, {"worker": WORKER_ID, "batch": batch})
    ids = [row[0] for row in result.all()]
    await db.commit()
    return ids


async def reap_stuck(db: AsyncSession) -> List[int]:
    """Return rows a worker died holding. Without this they are claimed once and never again."""
    result = await db.execute(REAP_SQL, {"stuck": STUCK_AFTER_SECONDS})
    ids = [row[0] for row in result.all()]
    await db.commit()
    return ids


# ── delivering ─────────────────────────────────────────────────────────────────────────────────

async def _still_authorised(
    db: AsyncSession, subscription: WebhookSubscription, payload: Dict[str, Any]
) -> Optional[str]:
    """Why this delivery must not go out, or None.

    Reloaded rather than read from the row that was enqueued, because the point is to notice a
    change that happened after the enqueue.
    """
    install = (
        await db.execute(
            select(ExtensionInstallation).where(
                ExtensionInstallation.id == subscription.installation_id
            )
        )
    ).scalar_one_or_none()
    if install is None:
        return "the installation no longer exists"
    if install.status != "active":
        return f"the installation is {install.status}"

    cluster_id = (payload.get("data") or {}).get("cluster_id")
    if cluster_id:
        from app.services import cluster_generations

        generations = await cluster_generations.current_or_none()
        if cluster_id not in cluster_generations.reachable(install.granted_scope, generations):
            return "the installation no longer reaches that cluster"
        from app.services.key_party_bound import install_grant_names, payload_reach

        # note: the installer's bound is judged when the event is queued (emitter.py). Here only the
        # stored grant, so an identity provider outage cannot drop a delivery, and a project deleted
        # after its instance events were queued does not cancel them (security review, 2026-11-06).
        about = payload_reach(payload)
        if about is not None and not (about[0] and install_grant_names(install, about[0], about[1])):
            return "the installation's grant no longer names that project"
    return None


async def deliver(
    db: AsyncSession, delivery: WebhookDelivery, vault_client, *, now: Optional[datetime] = None
) -> str:
    """Send one delivery and record what happened. Returns the resulting state."""
    now = now or now_utc()
    subscription = (
        await db.execute(
            select(WebhookSubscription).where(WebhookSubscription.id == delivery.subscription_id)
        )
    ).scalar_one_or_none()

    if subscription is None:
        delivery.state = "dropped"
        delivery.last_error = "the subscription no longer exists"
        await db.commit()
        return delivery.state

    refusal = await _still_authorised(db, subscription, delivery.payload or {})
    if refusal is not None:
        # Recorded, not silently discarded. An operator asking why an add-on stopped hearing about
        # a cluster gets an answer.
        delivery.state = "dropped"
        delivery.last_error = refusal
        await db.commit()
        logger.info("Dropped delivery %s: %s", delivery.id, refusal)
        return delivery.state

    # important: every currently valid secret, not just the newest. The header is a space
    # separated list for exactly this reason, and the subscription row has carried
    # `previous_secret_expires_at` since it was created, but only one secret was ever sent. So a
    # rotation broke every receiver on its very next delivery rather than giving it a window to
    # switch in, which is the failure the list format exists to prevent.
    secrets_ = read_signing_secrets(vault_client, subscription.installation_id)
    if not secrets_:
        delivery.state = "dropped"
        delivery.last_error = "no signing secret is stored for this installation"
        await db.commit()
        return delivery.state

    import json

    body = json.dumps(delivery.payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    timestamp = int(now.timestamp())
    headers = {
        "content-type": "application/json",
        "user-agent": "Orcastra-Webhook/1",
        SIGNATURE_HEADER: signature_header(secrets_, timestamp, body),
        TIMESTAMP_HEADER: str(timestamp),
        EVENT_ID_HEADER: delivery.event_id,
        EVENT_TYPE_HEADER: delivery.event_type,
        DELIVERY_ID_HEADER: f"dlv_{delivery.id}",
    }

    started = time.monotonic()
    delivery.attempt += 1
    try:
        status, head, seen = await post_pinned(
            subscription.endpoint_url,
            content=body,
            headers=headers,
            max_response_bytes=MAX_RESPONSE_BYTES,
        )
    except SafeHTTPError as exc:
        return await _record_failure(db, delivery, subscription, str(exc), now)

    delivery.response_ms = int((time.monotonic() - started) * 1000)
    delivery.response_status = status

    # important: the excerpt is kept only when the endpoint is public. Storing what an internal
    # address replied would turn a blind SSRF into a returned one, which is the mistake the
    # organization refresh path already documents.
    if describe_url_risk(subscription.endpoint_url, require_https=True) is None:
        delivery.response_excerpt = head[:256].decode("utf-8", errors="replace")
    else:
        delivery.response_excerpt = f"<{seen} bytes, not recorded>"

    if 200 <= status < 300:
        delivery.state = "delivered"
        delivery.last_error = None
        subscription.consecutive_failures = 0
        subscription.last_success_at = now
        await db.commit()
        return delivery.state

    return await _record_failure(db, delivery, subscription, f"endpoint returned {status}", now)


async def _record_failure(
    db: AsyncSession,
    delivery: WebhookDelivery,
    subscription: WebhookSubscription,
    reason: str,
    now: datetime,
) -> str:
    delivery.last_error = reason
    subscription.consecutive_failures = (subscription.consecutive_failures or 0) + 1
    subscription.last_failure_at = now
    subscription.last_failure_reason = reason

    if delivery.attempt >= MAX_ATTEMPTS:
        delivery.state = "dead"
    else:
        delivery.state = "pending"
        delivery.next_attempt_at = now + timedelta(
            seconds=_next_attempt_delay(delivery.attempt)
        )

    if subscription.consecutive_failures >= AUTO_SUSPEND_AFTER:
        # An endpoint that has refused this many in a row is gone, and continuing to POST at it
        # is us behaving like a reflector rather than a sender.
        subscription.status = "suspended"
        subscription.suspended_reason = (
            f"{subscription.consecutive_failures} consecutive failures: {reason}"
        )
        logger.warning(
            "Suspended webhook subscription %s after repeated failure", subscription.id
        )

    delivery.locked_by = None
    delivery.locked_at = None
    await db.commit()
    return delivery.state


# ── the loop ───────────────────────────────────────────────────────────────────────────────────

_task: Optional[asyncio.Task] = None


async def dispatch_once(session_factory, vault_factory, *, batch: int = 16) -> int:
    """One pass. Returns how many deliveries were attempted."""
    async with session_factory() as db:
        await reap_stuck(db)
        ids = await claim_batch(db, batch=batch)
    if not ids:
        return 0

    vault_client = vault_factory()
    attempted = 0
    for delivery_id in ids:
        async with session_factory() as db:
            delivery = (
                await db.execute(
                    select(WebhookDelivery).where(WebhookDelivery.id == delivery_id)
                )
            ).scalar_one_or_none()
            if delivery is None:
                continue
            try:
                await deliver(db, delivery, vault_client)
            except Exception:
                logger.warning("Delivery %s raised", delivery_id, exc_info=True)
            attempted += 1
    return attempted


async def dispatch_loop(session_factory, vault_factory, *, interval: float = 2.0) -> None:
    while True:
        try:
            await dispatch_once(session_factory, vault_factory)
        except Exception as exc:  # one bad pass must not kill the loop
            logger.warning("Webhook dispatch pass failed: %s", exc)
        await asyncio.sleep(interval)


def start_dispatcher(session_factory, vault_factory) -> None:
    """Idempotent: safe to call more than once per worker."""
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(dispatch_loop(session_factory, vault_factory))
        logger.info("Webhook dispatcher started as %s", WORKER_ID)
