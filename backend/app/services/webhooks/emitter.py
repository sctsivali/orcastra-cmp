"""Selecting the subscriptions an event belongs to and enqueueing it.

An INSTALLATION event (suspended, resumed, scope changed) is about a row this deployment owns, so
it is enqueued inside the caller's transaction: a rolled-back suspension cannot leave a delivery
claiming it happened. A RESOURCE event (an instance created, a volume deleted) follows a write LXD
already took, so there is no transaction to join: it opens and commits its own session, and must
never turn a successful operation into a failed response.

important: a resource event is enqueued only for installations whose stored grant still covers the
cluster. This is the cheap filter; `_still_authorised` re-checks at delivery, since the grant can
change between enqueue and send, and that check is the boundary.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Collection, List, Optional, Sequence

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

from app.services import cluster_generations
from app.services.webhooks.reach import (
    EVENT_REACH_SECONDS,
    PROJECT_DELETED,
    ReachMemo,
    install_grant_names,
    install_reaches_project,
    read_bounds,
)

logger = logging.getLogger(__name__)

__all__ = [
    "EVENT_FOR_OPERATION",
    "emit_for_proxy_operation",
    "emit_installation_event",
    "emit_resource_event",
]

# Which proxy operation announces which event, and what the resource is called.
#
# Only operations that create or remove something: a lifecycle subscriber does not want a delivery
# every time a container starts. `EXTENSION_EVENTS` is the catalogue and this is the part of it the
# proxy can source. The proxy vocabulary has no volume operation, so volume events come from the
# storage volume router.
EVENT_FOR_OPERATION = {
    "create_instance": ("orcastra.instance.created.v1", "instance"),
    "delete_instance": ("orcastra.instance.deleted.v1", "instance"),
    "create_project": ("orcastra.project.created.v1", "project"),
    "delete_project": ("orcastra.project.deleted.v1", "project"),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _subscriptions_for_cluster(
    db: AsyncSession,
    cluster_id: str,
    event_type: str,
    project: Optional[str] = None,
    reached_organizations: Collection[int] = (),
) -> List[WebhookSubscription]:
    """Active subscriptions on active installations whose grant still covers this cluster.

    The cluster filter runs in Python: `granted_scope` is a list of grant objects, and a JSONB `@>`
    against a partial object is easy to get wrong. The rows are the deployment's active
    subscriptions, so the set stays small.
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

    # important: only grants for a registration still active (services/cluster_generations.py). A
    # grant left from an ended registration names an id another organization may hold now, and
    # routing on it would send that organization's events to this add-on.
    generations = await cluster_generations.current_or_none()
    deleted = event_type == PROJECT_DELETED

    def held(install: Any) -> Optional[bool]:
        # A deleted project is out of every bound by the time its event goes out, so the
        # organizations whose policies held it are named by the delete itself.
        return (getattr(install, "organization_id", None) in reached_organizations) if deleted else None

    candidates = []
    for subscription, install in rows:
        if event_type not in (subscription.event_types or []):
            continue
        if cluster_id not in cluster_generations.reachable(install.granted_scope, generations):
            continue
        # important: the event's project must be one the installation reaches, its grant held to
        # its installer's bound (services/key_party_bound.py). Otherwise an add-on a Partner
        # installed on a shared cluster would hear the other party's project names.
        if project is not None and not install_grant_names(install, cluster_id, project):
            continue
        candidates.append((subscription, install))
    if project is None:
        return [subscription for subscription, _install in candidates]

    memo: ReachMemo = {}
    await read_bounds(
        [(install, cluster_id) for _sub, install in candidates if not held(install)], memo, EVENT_REACH_SECONDS
    )
    interested: List[WebhookSubscription] = []
    for subscription, install in candidates:
        if await _reaches_project(install, cluster_id, project, held(install), memo):
            interested.append(subscription)
    return interested


async def _reaches_project(
    install: Any, cluster_id: str, project: str, held: Optional[bool], memo: ReachMemo
) -> bool:
    # The bounds are already in the memo (read_bounds), so this does no I/O of its own.
    try:
        return await install_reaches_project(install, cluster_id, project, deleted_and_held=held, memo=memo)
    except Exception as exc:  # noqa: BLE001 - not knowing whether it reaches the project is not a yes
        logger.warning("Could not tell whether an installation reaches %s on %s: %s", project, cluster_id, sanitize_error(exc))
        return False



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
    reached_organizations: Collection[int] = (),
) -> int:
    """Enqueue one lifecycle event for every entitled subscription. Returns rows created.

    `reached_organizations`: for a project delete, the organizations whose policies held the
    project before the delete revoked them; their installations hear it although it has left
    their bound.

    important: never raises unless `raise_on_error` is set for a durable caller that retries. It
    runs after an operation that already changed a customer's cluster, so a failure here must not
    reach the caller as a failed create. The failure is logged; the reconcile sweep does not
    recover lost events.
    """
    from app.core.database import async_session_maker

    try:
        async with async_session_maker() as db:
            # A project event is about the project it names; anything else about the one holding it.
            event_project = resource_name if resource_type == "project" else (project or "default")
            subscriptions = await _subscriptions_for_cluster(
                db, cluster_id, event_type, event_project, reached_organizations
            )
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

    Errors propagate: the enqueue shares the transaction with the change it describes, so a failed
    enqueue rolls the change back with it, and an add-on is never told about a suspension that did
    not happen.
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
    # filters on `data.cluster_id`. Without one from the caller, the installation's first remaining
    # grant is used.
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

    Kept beside the map rather than in the router, so a new lifecycle operation is one edit.
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
