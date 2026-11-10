"""Everything a project delete owes once the project is gone, built from its follow-up row.

The request that deleted the project and the sweeper that settles a delete after a restart send the
same announcement from the same row, so neither depends on what the other process remembered.

important: every step is keyed by the row's `event_id`. The webhook skips subscriptions that already
have a delivery for it and OrcaHub gets a stable per-organization id, so a retry after a partial
announcement repeats neither. The audit line goes last, once both have gone out.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Collection

from app.core.cache import get_cache_manager
from app.services import project_delete_store as store
from app.services.activity_logger import get_activity_logger

logger = logging.getLogger(__name__)

ANNOUNCE_RETRY = timedelta(seconds=30)
MAX_ANNOUNCE_ATTEMPTS = 20


def clear_caches(origin: str) -> None:
    """The lists a project delete changes; the proxy's also cover what an external write touches."""
    cache = get_cache_manager()
    # Clears same-name quota data as well, so a later recreate cannot inherit it.
    cache.invalidate_project_caches()
    if origin == "proxy":
        cache.invalidate_after_external_write()


async def announce(
    followup: store.Followup, *, settled_later: bool, reached_organizations: Collection[int] = ()
) -> None:
    """Webhook, then OrcaHub and the audit success for a dashboard delete. Raises on failure."""
    from app.services.orcahub_webhook import emit_orcahub_event_durable
    from app.services.webhooks.emitter import emit_resource_event

    host, name = followup.cluster_id, followup.project_name
    await emit_resource_event(
        event_type="orcastra.project.deleted.v1",
        cluster_id=host,
        resource_type="project",
        resource_name=name,
        # A project event is about the project it names; the caller's own project is not part of it.
        project=None,
        event_id=f"evt_{followup.event_id}",
        raise_on_error=True,
        reached_organizations=reached_organizations,
    )
    if followup.origin != "route":
        return
    await emit_orcahub_event_durable(
        event_id=followup.event_id,
        event_type="project.deleted",
        cluster_id=host,
        project=name,
        name=name,
        resource_type="PROJECT",
        namespace="orcastra-project-delete",
        reached_organizations=reached_organizations,
    )
    get_activity_logger().log_activity(
        action="project.delete",
        target_type="project",
        target_id=name,
        target_host=host,
        result="success",
        details={"settled_after_answer": True} if settled_later else None,
        actor_user_id=followup.requester,
        event_id=f"evt_{followup.event_id}_audit",
    )


async def announce_and_finish(
    followup: store.Followup, *, settled_later: bool, reached_organizations: Collection[int] = ()
) -> None:
    """Announce, then close the row; a failure leaves it settled for the sweeper to retry."""
    try:
        await announce(followup, settled_later=settled_later, reached_organizations=reached_organizations)
    except Exception as exc:  # noqa: BLE001 - retried from the row, never lost with this task
        error = f"{type(exc).__name__}: {exc}"[:500]
        if followup.id is None:
            logger.warning(
                "Announcing the delete of %s on %s failed and has no row to retry from: %s",
                followup.project_name, followup.cluster_id, error,
            )
            return
        if followup.attempts >= MAX_ANNOUNCE_ATTEMPTS:
            logger.error(
                "Giving up announcing the delete of %s on %s after %d attempts: %s",
                followup.project_name, followup.cluster_id, followup.attempts, error,
            )
            await store.mark_done(followup.id, error=error)
            return
        logger.warning(
            "Announcing the delete of %s on %s failed; retrying: %s",
            followup.project_name, followup.cluster_id, error,
        )
        await store.retry_later(followup.id, ANNOUNCE_RETRY, error=error)
        return
    await store.mark_done(followup.id)
