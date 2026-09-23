"""Finding the subscriptions an event belongs to, which is the half that was missing.

`enqueue_event` takes the subscriptions as a parameter, so somebody has to select them. Nobody
did. The result was a platform where nine events were catalogued, served to add-on authors
through `GET /extensions/self`, and documented publicly with a "when it fires" column, while
`enqueue_event` had no caller anywhere and no event ever fired. A subscription verified, went
active, showed a fingerprint, and then delivered nothing for the rest of its life.

Two kinds of event, and they differ in a way that matters more than it looks.

An INSTALLATION event (suspended, resumed, scope changed) is about a row this deployment owns, so
it is enqueued inside the caller's transaction. That is what `enqueue_event`'s docstring asks for
and it gives the property worth having: the event exists exactly when the change does, and a
rolled-back suspension cannot leave a delivery claiming it happened.

A RESOURCE event (an instance created, a volume deleted) is about something that happened on a
cluster, not in Postgres. There is no transaction to join, because the authoritative write already
happened over the network to LXD. So it opens its own session and commits, and it must never turn
a successful operation into a failed response: the instance exists whether or not we managed to
tell anybody about it. Losing an event is bad; losing the caller's instance because the event
system had a bad day is worse.

important: a resource event is enqueued only for installations whose stored grant still covers the
cluster. `_still_authorised` re-checks the same thing at delivery time, deliberately, because the
grant can change between enqueue and send. This is the cheap filter, not the boundary.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_sanitizer import sanitize_error
from app.models.extension_models import (
    ExtensionInstallation,
    WebhookDelivery,
    WebhookSubscription,
)

from .dispatcher import enqueue_event
from .envelope import ResourceEvent

logger = logging.getLogger(__name__)

__all__ = [
    "EVENT_FOR_OPERATION",
    "emit_for_proxy_operation",
    "emit_installation_event",
    "emit_resource_event",
]

# Which proxy operation announces which event, and what the resource is called.
#
# important: this is deliberately NOT every operation. An add-on subscribing to lifecycle wants to
# know a thing came into or went out of existence; it does not want a delivery every time somebody
# starts a container. `EXTENSION_EVENTS` is the catalogue and this is the half of it the proxy can
# source. Volumes are absent here on purpose: there is no volume operation in the proxy
# vocabulary, so those two events are sourced from the dashboard router instead.
EVENT_FOR_OPERATION = {
    "create_instance": ("orcastra.instance.created.v1", "instance"),
    "delete_instance": ("orcastra.instance.deleted.v1", "instance"),
    "create_project": ("orcastra.project.created.v1", "project"),
    "delete_project": ("orcastra.project.deleted.v1", "project"),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _subscriptions_for_cluster(
    db: AsyncSession, cluster_id: str, event_type: str
) -> List[WebhookSubscription]:
    """Active subscriptions on active installations whose grant still covers this cluster.

    The cluster filter is applied in Python rather than as a JSONB containment query because
    `granted_scope` is a list of grant objects rather than a list of ids, and a `@>` against a
    partial object is easy to get subtly wrong. The row count here is the number of active
    subscriptions in the deployment, not the number of instances, so this stays small.
    """
    rows = (
        await db.execute(
            select(WebhookSubscription, ExtensionInstallation)
            .join(
                ExtensionInstallation,
                ExtensionInstallation.id == WebhookSubscription.installation_id,
            )
            .where(
                WebhookSubscription.status == "active",
                ExtensionInstallation.status == "active",
            )
        )
    ).all()

    interested: List[WebhookSubscription] = []
    for subscription, install in rows:
        if event_type not in (subscription.event_types or []):
            continue
        reachable = {
            grant.get("cluster_id")
            for grant in (install.granted_scope or [])
            if isinstance(grant, dict)
        }
        if cluster_id in reachable:
            interested.append(subscription)
    return interested


async def emit_resource_event(
    *,
    event_type: str,
    cluster_id: str,
    resource_type: str,
    resource_name: str,
    project: Optional[str] = None,
    state: Optional[str] = None,
    operation_id: Optional[str] = None,
    event_id: Optional[str] = None,
    occurred_at: Optional[str] = None,
    raise_on_error: bool = False,
) -> int:
    """Enqueue one lifecycle event for every entitled subscription. Returns rows created.

    important: this never raises. It is called after an operation that already changed a customer's
    cluster, so a failure here must not be reported to the caller as a failed create. The failure
    is logged instead, and the reconcile sweep is not involved because a lost event is not
    recoverable state, it is a lost notification.
    """
    from app.core.database import async_session_maker

    try:
        async with async_session_maker() as db:
            subscriptions = await _subscriptions_for_cluster(db, cluster_id, event_type)
            if not subscriptions:
                return 0
            stable_event_id = event_id or f"evt_{uuid.uuid4()}"
            if event_id:
                # A migration outbox can be reclaimed after this transaction committed but
                # before its own row was acknowledged. Reusing the outbox id and excluding
                # existing subscription deliveries makes that retry idempotent.
                existing_subscription_ids = set(
                    (
                        await db.execute(
                            select(WebhookDelivery.subscription_id).where(
                                WebhookDelivery.event_id == stable_event_id
                            )
                        )
                    ).scalars().all()
                )
                subscriptions = [
                    subscription
                    for subscription in subscriptions
                    if subscription.id not in existing_subscription_ids
                ]
                if not subscriptions:
                    return 0
            event = ResourceEvent(
                event_id=stable_event_id,
                event_type=event_type,
                occurred_at=occurred_at or _now(),
                cluster_id=cluster_id,
                resource_type=resource_type,
                resource_name=resource_name,
                project=project,
                state=state,
                operation_id=operation_id,
            )
            created = await enqueue_event(db, event, subscriptions=subscriptions)
            await db.commit()
            return len(created)
    except Exception as exc:  # noqa: BLE001 - optionally retried by a durable caller
        if raise_on_error:
            raise
        logger.warning(
            "Could not enqueue %s for %s on %s: %s",
            event_type,
            resource_name,
            cluster_id,
            sanitize_error(exc),
        )
        return 0


async def emit_installation_event(
    db: AsyncSession,
    *,
    event_type: str,
    installation: ExtensionInstallation,
    cluster_id: Optional[str] = None,
) -> int:
    """Enqueue an event about an installation itself, in the caller's transaction.

    Deliberately not swallowing: this one shares the transaction with the change it describes, so
    if the enqueue fails the caller's rollback takes the change with it, and an add-on is never
    told it was suspended by a suspension that did not happen.
    """
    subscriptions: Sequence[WebhookSubscription] = (
        (
            await db.execute(
                select(WebhookSubscription).where(
                    WebhookSubscription.installation_id == installation.id,
                    WebhookSubscription.status == "active",
                )
            )
        )
        .scalars()
        .all()
    )
    if not subscriptions:
        return 0

    # An installation event still needs a cluster on the envelope, because `_still_authorised`
    # filters on `data.cluster_id`. The installation's first remaining grant is the honest answer
    # when the caller does not name one.
    if cluster_id is None:
        grants = [g for g in (installation.granted_scope or []) if isinstance(g, dict)]
        cluster_id = str(grants[0].get("cluster_id")) if grants else ""

    event = ResourceEvent(
        event_id=f"evt_{uuid.uuid4()}",
        event_type=event_type,
        occurred_at=_now(),
        cluster_id=cluster_id,
        resource_type="installation",
        resource_name=str(installation.id),
        state=installation.status,
    )
    created = await enqueue_event(db, event, subscriptions=list(subscriptions))
    return len(created)


async def emit_for_proxy_operation(
    operation: str,
    *,
    cluster_id: str,
    project: Optional[str],
    params: Optional[dict],
    result: object,
) -> int:
    """Announce a proxy operation that changed what exists, if it is one of those.

    Kept beside the map rather than in the router so the vocabulary and its use stay together, and
    so a new lifecycle operation is one edit rather than two.
    """
    mapped = EVENT_FOR_OPERATION.get(operation)
    if mapped is None:
        return 0
    event_type, resource_type = mapped

    name = str((params or {}).get("name") or "")
    state = None
    operation_id = None
    if isinstance(result, dict):
        name = str(result.get("name") or name)
        state = result.get("status") or None
        operation_id = result.get("operation_id") or None
    if not name:
        return 0

    return await emit_resource_event(
        event_type=event_type,
        cluster_id=cluster_id,
        resource_type=resource_type,
        resource_name=name,
        project=project,
        state=state,
        operation_id=operation_id,
    )
