"""Subscribing an installation to events, and looking at what was delivered.

A subscription does not go live when it is created. It answers `pending_verification` until the
endpoint has proved it wants the traffic by returning 2xx to a signed ping. Without that step the
sender is a reflector: anyone could register a victim's URL and aim events at it from this server's
address.
"""

import logging
from datetime import datetime, timedelta, timezone
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_key_expiry import now_utc, utc_isoformat
from app.core.config import webhook_requires_https
from app.core.database import get_db
from app.core.dependencies import AuthenticatedUser, check_read_only_mode, require_partner
from app.core.extension_manifest import EXTENSION_EVENTS
from app.core.org_scope import org_scope_dependency
from app.core.rbac import is_admin
from app.core.url_safety import assert_url_is_fetchable
from app.models.extension_models import (
    ExtensionInstallation,
    WebhookDelivery,
    WebhookSubscription,
)
from app.routers.integrations import get_vault_integrations_client
from app.services.activity_logger import get_activity_logger
from app.services.extension_credential_guard import orgs_the_user_runs
from app.services.extension_credentials import (
    SIGNING_OVERLAP_SECONDS,
    read_signing_secret,
    rotate_signing_secret,
)
from app.services.webhooks import dispatcher
from app.services.webhooks.envelope import ResourceEvent
from app.services.webhooks.signing import secret_fingerprint

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

# What a delivery the installation does not reach shows in place of its payload.
REDACTED_PAYLOAD = {"redacted": True, "reason": "This event is about a project this installation does not reach."}

router = APIRouter(
    prefix="/extensions",
    tags=["Extensions"],
    dependencies=[Depends(require_partner()), Depends(org_scope_dependency)],
)


class SubscriptionRequest(BaseModel):
    endpoint_url: str = Field(..., max_length=512)
    event_types: List[str] = Field(..., min_length=1, max_length=len(EXTENSION_EVENTS))

    @field_validator("endpoint_url")
    @classmethod
    def _endpoint_is_one_this_server_may_call(cls, value: str) -> str:
        # The same guard every stored-then-contacted URL gets, plus a port policy this surface
        # chooses for itself. The refusal names a category and never an address.
        return assert_url_is_fetchable(
            value, field="endpoint_url", require_https=webhook_requires_https()
        )

    @field_validator("event_types")
    @classmethod
    def _events_are_in_the_catalogue(cls, value: List[str]) -> List[str]:
        unknown = sorted(set(value) - set(EXTENSION_EVENTS))
        if unknown:
            raise ValueError(f"unknown events: {', '.join(unknown)}")
        return sorted(set(value))


class SubscriptionResponse(BaseModel):
    id: int
    installation_id: int
    endpoint_url: str
    event_types: List[str]
    status: str
    secret_fingerprint: str
    verified_at: Optional[Any] = None
    consecutive_failures: int
    last_success_at: Optional[Any] = None
    last_failure_at: Optional[Any] = None
    last_failure_reason: Optional[str] = None
    suspended_reason: Optional[str] = None


class SubscriptionSecretResponse(BaseModel):
    subscription_id: int
    signing_secret: str
    secret_fingerprint: str


class DeliveryPage(BaseModel):
    """One page of the delivery log, with two totals.

    `total` counts what the current state filter matches, for paging. `counts` breaks down every
    delivery on the installation by state, ignoring the filter, for the health panel.
    """

    deliveries: List[Dict[str, Any]]
    total: int
    counts: Dict[str, int]
    limit: int
    offset: int
    has_more: bool


def _to_response(row: WebhookSubscription) -> SubscriptionResponse:
    return SubscriptionResponse(
        id=row.id,
        installation_id=row.installation_id,
        endpoint_url=row.endpoint_url,
        event_types=list(row.event_types or []),
        status=row.status,
        secret_fingerprint=row.secret_fingerprint,
        verified_at=row.verified_at,
        consecutive_failures=row.consecutive_failures or 0,
        last_success_at=row.last_success_at,
        last_failure_at=row.last_failure_at,
        last_failure_reason=row.last_failure_reason,
        suspended_reason=row.suspended_reason,
    )


async def _install_for(
    db: AsyncSession, install_id: int, user: AuthenticatedUser
) -> ExtensionInstallation:
    install = (
        await db.execute(
            select(ExtensionInstallation).where(ExtensionInstallation.id == install_id)
        )
    ).scalar_one_or_none()
    if install is None:
        raise HTTPException(status_code=404, detail="Installation not found")
    if not is_admin(user):
        if install.organization_id not in await orgs_the_user_runs(db, user.email):
            raise HTTPException(status_code=404, detail="Installation not found")
    return install


async def _subscription_for(
    db: AsyncSession, install_id: int, subscription_id: int, user: AuthenticatedUser
) -> WebhookSubscription:
    await _install_for(db, install_id, user)
    row = (
        await db.execute(
            select(WebhookSubscription).where(
                WebhookSubscription.id == subscription_id,
                WebhookSubscription.installation_id == install_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return row


@router.post(
    "/installations/{install_id}/webhooks",
    response_model=SubscriptionSecretResponse,
    status_code=201,
    summary="Subscribe an installation to events",
    description=(
        "Returns the signing secret once. The subscription stays pending until the endpoint "
        "answers a signed verification ping."
    ),
)
async def create_subscription(
    install_id: int,
    request: SubscriptionRequest,
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
) -> SubscriptionSecretResponse:
    install = await _install_for(db, install_id, user)
    vault_client = get_vault_integrations_client()

    secret = read_signing_secret(vault_client, install.id) or rotate_signing_secret(
        vault_client, install.id
    )
    row = WebhookSubscription(
        installation_id=install.id,
        endpoint_url=request.endpoint_url,
        event_types=request.event_types,
        secret_fingerprint=secret_fingerprint(secret),
        status="pending_verification",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)

    activity_logger.log_activity(
        action="extension.webhook.subscribe",
        target_type="extension_installation",
        target_id=str(install.id),
        target_organization_id=install.organization_id,
        details={"subscription_id": row.id, "events": request.event_types},
    )
    return SubscriptionSecretResponse(
        subscription_id=row.id, signing_secret=secret, secret_fingerprint=row.secret_fingerprint
    )


@router.get(
    "/installations/{install_id}/webhooks",
    response_model=List[SubscriptionResponse],
    summary="List an installation's subscriptions",
)
async def list_subscriptions(
    install_id: int,
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
) -> List[SubscriptionResponse]:
    await _install_for(db, install_id, user)
    rows = (
        await db.execute(
            select(WebhookSubscription)
            .where(WebhookSubscription.installation_id == install_id)
            .order_by(WebhookSubscription.id)
        )
    ).scalars().all()
    # The secret is never in this shape, only its fingerprint.
    return [_to_response(row) for row in rows]


@router.post(
    "/installations/{install_id}/webhooks/{subscription_id}/verify",
    response_model=SubscriptionResponse,
    summary="Prove the endpoint wants this traffic",
    description=(
        "Sends one signed ping. The subscription goes active only if the endpoint answers 2xx, "
        "which is what stops this sender being pointed at somebody who never agreed to receive."
    ),
)
async def verify_subscription(
    install_id: int,
    subscription_id: int,
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
) -> SubscriptionResponse:
    row = await _subscription_for(db, install_id, subscription_id, user)

    ping = ResourceEvent(
        event_id=f"evt_verify_{uuid.uuid4().hex[:16]}",
        event_type="orcastra.subscription.verify.v1",
        occurred_at=utc_isoformat(now_utc()),
        cluster_id="",
        resource_type="SUBSCRIPTION",
        resource_name=str(row.id),
    )
    delivery = WebhookDelivery(
        subscription_id=row.id,
        event_id=ping.event_id,
        event_type=ping.event_type,
        payload={
            "specversion": "1.0",
            "id": ping.event_id,
            "type": ping.event_type,
            "time": ping.occurred_at,
            "subscription_id": row.id,
            "installation_id": row.installation_id,
            "sequence": 0,
            "data": {"resource_type": "SUBSCRIPTION", "resource_name": str(row.id)},
        },
        state="pending",
        next_attempt_at=now_utc(),
    )
    db.add(delivery)
    await db.commit()
    await db.refresh(delivery)

    # A verification ping goes out now rather than through the queue: the operator is waiting on
    # the answer.
    state = await dispatcher.deliver(db, delivery, get_vault_integrations_client())
    if state == "delivered":
        row.status = "active"
        row.verified_at = now_utc()
        row.consecutive_failures = 0
    else:
        row.status = "pending_verification"
        row.last_failure_reason = delivery.last_error
        # important: a verification ping is one shot. On failure `deliver` leaves the row
        # `pending` for a retry, and the queue would keep posting at an endpoint that just
        # declined the traffic. A failed verification also must not count towards the failure
        # budget of a subscription that was never active.
        delivery.state = "dead"
        delivery.next_attempt_at = None
        row.consecutive_failures = 0
    await db.commit()
    await db.refresh(row)
    return _to_response(row)


@router.post(
    "/installations/{install_id}/webhooks/{subscription_id}/rotate-secret",
    response_model=SubscriptionSecretResponse,
    summary="Issue a new signing secret for this installation",
    description=(
        "The signing secret belongs to the installation, not to one subscription, so this "
        "changes the secret every endpoint on this installation is signed with. Any other "
        "receiver must be given the new value or it will start rejecting deliveries."
    ),
)
async def rotate_secret(
    install_id: int,
    subscription_id: int,
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
) -> SubscriptionSecretResponse:
    row = await _subscription_for(db, install_id, subscription_id, user)
    secret = rotate_signing_secret(get_vault_integrations_client(), row.installation_id)

    # important: the secret is per installation, and an installation can hold several
    # subscriptions. Restamp every sibling, or they advertise a fingerprint for a secret that is
    # gone. Deliveries stay correct either way: the signature header's fingerprint is computed
    # from the secret at send time.
    fingerprint = secret_fingerprint(secret)
    siblings = (
        await db.execute(
            select(WebhookSubscription).where(
                WebhookSubscription.installation_id == row.installation_id
            )
        )
    ).scalars().all()
    # The overlap window, recorded so an operator can see how long the previous secret keeps
    # working.
    overlap_until = datetime.now(timezone.utc) + timedelta(seconds=SIGNING_OVERLAP_SECONDS)
    for subscription in siblings:
        subscription.secret_fingerprint = fingerprint
        subscription.previous_secret_expires_at = overlap_until
    await db.commit()
    activity_logger.log_activity(
        action="extension.webhook.secret.rotate",
        target_type="extension_installation",
        target_id=str(install_id),
        details={
            "subscription_id": row.id,
            # Every sibling subscription this rotation also changed.
            "subscriptions_resigned": sorted(s.id for s in siblings),
            "previous_secret_valid_until": overlap_until.isoformat(),
        },
    )
    return SubscriptionSecretResponse(
        subscription_id=row.id, signing_secret=secret, secret_fingerprint=row.secret_fingerprint
    )


@router.delete(
    "/installations/{install_id}/webhooks/{subscription_id}",
    status_code=204,
    summary="Stop delivering to an endpoint",
)
async def delete_subscription(
    install_id: int,
    subscription_id: int,
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
) -> None:
    row = await _subscription_for(db, install_id, subscription_id, user)
    await db.delete(row)
    await db.commit()
    activity_logger.log_activity(
        action="extension.webhook.unsubscribe",
        target_type="extension_installation",
        target_id=str(install_id),
        details={"subscription_id": subscription_id},
    )


@router.get(
    "/installations/{install_id}/deliveries",
    response_model=DeliveryPage,
    summary="What was delivered, and what happened",
    description=(
        "The answer to 'what did this add-on get told about my organization'. The signing secret "
        "never appears here; the signature header does, and a signature is safe to show."
    ),
)
async def list_deliveries(
    install_id: int,
    state: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
) -> DeliveryPage:
    """A page of the log, newest first.

    Delivery is at-least-once and every retry writes a row, so the log grows quickly. It is paged
    with `offset`, and the state filter applies before paging.
    """
    install = await _install_for(db, install_id, user)
    subscription_ids = [
        row[0]
        for row in (
            await db.execute(
                select(WebhookSubscription.id).where(
                    WebhookSubscription.installation_id == install_id
                )
            )
        ).all()
    ]
    if not subscription_ids:
        return DeliveryPage(
            deliveries=[], total=0, counts={}, limit=limit, offset=offset, has_more=False
        )

    mine = WebhookDelivery.subscription_id.in_(subscription_ids)

    # Not filtered by state: the health panel reads these counts for the whole installation.
    counts = {
        row[0]: row[1]
        for row in (
            await db.execute(
                select(WebhookDelivery.state, func.count())
                .where(mine)
                .group_by(WebhookDelivery.state)
            )
        ).all()
    }

    window = mine if not state else and_(mine, WebhookDelivery.state == state)
    total = (
        await db.execute(select(func.count()).select_from(WebhookDelivery).where(window))
    ).scalar_one()

    rows = (
        (
            await db.execute(
                select(WebhookDelivery)
                .where(window)
                .order_by(WebhookDelivery.id.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    # important: a stored delivery can carry another party's names, so a row whose payload this
    # installation does not reach is shown without it.
    from app.services.webhooks.reach import REACH_SECONDS, payload_reach, payload_reaches, read_bounds

    memo: Dict[Any, Any] = {}
    clusters = {about[0] for about in (payload_reach(row.payload or {}) for row in rows) if about and about[0]}
    await read_bounds([(install, cluster_id) for cluster_id in sorted(clusters)], memo, REACH_SECONDS)
    shown = {row.id: await payload_reaches(install, row.payload or {}, memo) for row in rows}
    return DeliveryPage(
        deliveries=[
            {
                "id": row.id,
                "subscription_id": row.subscription_id,
                "event_id": row.event_id,
                "event_type": row.event_type,
                "state": row.state,
                "attempt": row.attempt,
                "response_status": row.response_status,
                "response_ms": row.response_ms,
                # A receiver's answer can echo what it was sent.
                "response_excerpt": row.response_excerpt if shown[row.id] else None,
                "last_error": row.last_error,
                "next_attempt_at": row.next_attempt_at,
                "created_at": row.created_at,
                "payload": row.payload if shown[row.id] else REDACTED_PAYLOAD,
            }
            for row in rows
        ],
        total=total,
        counts=counts,
        limit=limit,
        offset=offset,
        has_more=offset + len(rows) < total,
    )


@router.post(
    "/installations/{install_id}/deliveries/{delivery_id}/redeliver",
    summary="Send a delivery again",
    description="A new attempt with the same event id, so a receiver's own dedupe still works.",
)
async def redeliver(
    install_id: int,
    delivery_id: int,
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    install = await _install_for(db, install_id, user)
    # important: bind the delivery to a subscription of this installation. A lookup by primary key
    # alone would let any partner holding an installation requeue any delivery by guessing an
    # integer, and the response returns its event_id.
    row = (
        await db.execute(
            select(WebhookDelivery)
            .join(WebhookSubscription, WebhookSubscription.id == WebhookDelivery.subscription_id)
            .where(
                WebhookDelivery.id == delivery_id,
                WebhookSubscription.installation_id == install_id,
            )
        )
    ).scalar_one_or_none()
    from app.services.webhooks.reach import payload_reaches

    if row is None or not await payload_reaches(install, row.payload or {}):
        raise HTTPException(status_code=404, detail="Delivery not found")

    row.state = "pending"
    row.attempt = 0
    row.next_attempt_at = now_utc()
    row.last_error = None
    await db.commit()
    activity_logger.log_activity(
        action="extension.webhook.redeliver",
        target_type="extension_installation",
        target_id=str(install_id),
        details={"delivery_id": delivery_id, "event_id": row.event_id},
    )
    return {"id": row.id, "state": row.state, "event_id": row.event_id}
