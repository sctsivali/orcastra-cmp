"""Durable, idempotent effects emitted only after authoritative target validation."""

from __future__ import annotations

import hashlib
from typing import Any

from app.core.cache import get_cache_manager
from app.models.migration_models import MigrationAuditOutbox, MigrationJob
from app.services.orcahub_webhook import emit_orcahub_event_durable
from app.services.webhooks.emitter import emit_resource_event


def _event_id(job_id: str, channel: str, event_type: str) -> str:
    material = f"{job_id}:{channel}:{event_type}".encode("utf-8")
    return f"mig_{hashlib.sha256(material).hexdigest()[:48]}"


def target_completion_events(
    job: MigrationJob,
    *,
    include_created_events: bool,
    occurred_at: str,
) -> list[dict[str, Any]]:
    """Build stable outbox rows for one validated target identity."""
    payload = {
        "cluster_id": job.target_cluster_id,
        "project": job.target_project,
        "resource_name": job.target_resource_name or job.source_resource_name,
        "resource_type": "instance" if job.scope == "instance" else "project",
        "occurred_at": occurred_at,
    }
    events: list[dict[str, Any]] = [
        {
            "event_id": _event_id(job.job_id, "cache", "migration.cache.invalidate"),
            "channel": "cache",
            "event_type": "migration.cache.invalidate",
            "phase": "verifying",
            "message": "Validated target inventory caches will be invalidated.",
            "extra": payload,
        }
    ]
    if include_created_events and job.scope == "instance":
        events.extend(
            [
                {
                    "event_id": _event_id(
                        job.job_id, "webhook", "orcastra.instance.created.v1"
                    ),
                    "channel": "webhook",
                    "event_type": "orcastra.instance.created.v1",
                    "phase": "verifying",
                    "message": "A validated target lifecycle event will be enqueued.",
                    "extra": payload,
                },
                {
                    "event_id": _event_id(
                        job.job_id, "billing", "instance.created"
                    ),
                    "channel": "billing",
                    "event_type": "instance.created",
                    "phase": "verifying",
                    "message": "A validated target ownership event will be delivered.",
                    "extra": payload,
                },
            ]
        )
    return events


def removal_events(
    job: MigrationJob, *, role: str, occurred_at: str
) -> list[dict[str, Any]]:
    """Stable events for an instance a migration deleted: the source at cleanup, the target
    at rollback. The role is part of every event id so the two never collide."""
    if role == "source":
        cluster_id, project = job.source_cluster_id, job.source_project
        name = job.source_resource_name
    else:
        cluster_id, project = job.target_cluster_id, job.target_project
        name = job.target_resource_name or job.source_resource_name
    payload = {
        "cluster_id": cluster_id,
        "project": project,
        "resource_name": name,
        "resource_type": "instance",
        "occurred_at": occurred_at,
    }
    return [
        {
            "event_id": _event_id(job.job_id, "cache", f"migration.cache.invalidate:{role}"),
            "channel": "cache",
            "event_type": "migration.cache.invalidate",
            "phase": "cleaning_up" if role == "source" else "rolling_back",
            "message": "Inventory caches will be invalidated after the deletion.",
            "extra": payload,
        },
        {
            "event_id": _event_id(job.job_id, "webhook", f"orcastra.instance.deleted.v1:{role}"),
            "channel": "webhook",
            "event_type": "orcastra.instance.deleted.v1",
            "phase": "cleaning_up" if role == "source" else "rolling_back",
            "message": "A deletion lifecycle event will be enqueued.",
            "extra": payload,
        },
        {
            "event_id": _event_id(job.job_id, "billing", f"instance.deleted:{role}"),
            "channel": "billing",
            "event_type": "instance.deleted",
            "phase": "cleaning_up" if role == "source" else "rolling_back",
            "message": "A deletion ownership event will be delivered.",
            "extra": payload,
        },
    ]


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Migration completion event is missing '{key}'.")
    return value


async def deliver_completion_event(row: MigrationAuditOutbox) -> bool:
    """Deliver a supported non-audit outbox row, returning whether it was handled."""
    payload = row.payload or {}
    if row.channel == "cache" and row.event_type == "migration.cache.invalidate":
        cache = get_cache_manager()
        cache.invalidate_instance_cache()
        # Moves change project usage, tag placement, and tenant visibility as well.
        cache.invalidate_resource_cache("projects", "project_usage")
        cache.invalidate_tag_cache()
        return True

    cluster_id = _required_text(payload, "cluster_id")
    project = _required_text(payload, "project")
    resource_name = _required_text(payload, "resource_name")
    occurred_at = _required_text(payload, "occurred_at")
    if row.channel == "webhook" and row.event_type in {
        "orcastra.instance.created.v1",
        "orcastra.instance.deleted.v1",
    }:
        await emit_resource_event(
            event_type=row.event_type,
            cluster_id=cluster_id,
            resource_type="instance",
            resource_name=resource_name,
            project=project,
            event_id=row.event_id,
            occurred_at=occurred_at,
            raise_on_error=True,
        )
        return True
    if row.channel == "billing" and row.event_type in {"instance.created", "instance.deleted"}:
        await emit_orcahub_event_durable(
            event_id=row.event_id,
            event_type=row.event_type,
            cluster_id=cluster_id,
            project=project,
            name=resource_name,
            occurred_at=occurred_at,
            resource_type="INSTANCE",
        )
        return True
    return False
