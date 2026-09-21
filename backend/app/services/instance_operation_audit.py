"""Durable audit delivery for instance create and specification jobs.

The job transaction writes a tiny outbox pointer. Delivery may happen more than once if a
process dies after emitting stdout but before marking the pointer delivered, so every retry uses
the same event ID. Downstream journal storage therefore deduplicates one logical event while the
job row remains the authoritative accepted/terminal state.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.database import async_session_maker
from app.core.error_sanitizer import sanitize_error
from app.models.db_models import InstanceOperationAuditOutbox, InstanceOperationJob
from app.services.activity_logger import get_activity_logger

logger = logging.getLogger(__name__)


def instance_operation_audit_event_id(job_id: str, stage: str) -> str:
    """Return the stable identity used by every delivery attempt for one logical event."""
    if stage not in {"accepted", "terminal"}:
        raise ValueError(f"Unsupported instance operation audit stage: {stage}")
    return hashlib.sha256(f"instance-operation:{job_id}:{stage}".encode()).hexdigest()


def enqueue_instance_operation_audit(
    db: AsyncSession,
    *,
    job_id: str,
    stage: str,
) -> None:
    """Add an outbox pointer to the caller's transaction without committing it."""
    db.add(
        InstanceOperationAuditOutbox(
            event_id=instance_operation_audit_event_id(job_id, stage),
            job_id=job_id,
            stage=stage,
        )
    )


def _audit_values(
    event: InstanceOperationAuditOutbox,
    job: InstanceOperationJob,
) -> dict[str, Any]:
    requested = dict(job.requested or {})
    common: dict[str, Any] = {
        "target_type": "instance",
        "target_id": job.instance_name,
        "target_host": job.cluster_id,
        "target_project": job.project,
        "actor_user_id": job.requester,
        "target_organization_id": job.target_organization_id,
        "target_organization_slug": job.target_organization_slug,
        "target_organization_resolution": job.target_organization_resolution,
        "event_id": event.event_id,
    }
    if event.stage == "accepted":
        if job.kind == "create":
            details = {
                "instance_type": job.instance_type,
                "profiles": requested.get("profiles") or ["default"],
                "project": job.project,
                "start_on_create": requested.get("start_on_create"),
                "job_id": job.job_id,
            }
            if requested.get("legacy_synchronous") is True:
                details["legacy_api"] = True
            action = "instance.create"
        else:
            details = {
                "job_id": job.job_id,
                "project": job.project,
                "requested": requested,
                "apply_mode": job.apply_mode,
            }
            action = "instance.specification.update.accepted"
        return {**common, "action": action, "result": "accepted", "details": details}

    status = job.status
    if status not in {"success", "failure", "partial"}:
        raise ValueError(
            f"Terminal audit for job {job.job_id} cannot use non-terminal status {status}"
        )
    operation_ids = list(job.operation_ids or [])
    details = {
        "job_id": job.job_id,
        "operation_id": job.operation_id,
        "operation_ids": operation_ids,
        "project": job.project,
        "status": status,
        "requested": requested,
    }
    values = {
        **common,
        "action": (
            "instance.create"
            if job.kind == "create"
            else "instance.specification.update"
        ),
        "result": "success" if status == "success" else "failure",
        "error_code": None if status == "success" else status.upper(),
        "error_message": job.error,
        "details": details,
        "after_state": job.result,
    }
    if job.kind == "specification":
        values["before_state"] = job.before_spec
    return values


async def drain_instance_operation_audits(
    *,
    job_id: Optional[str] = None,
    limit: int = 100,
) -> int:
    """Deliver pending pointers and return the number marked delivered.

    Rows stay locked until their emit outcome is committed. Concurrent API workers use
    ``SKIP LOCKED`` so each pass remains bounded and never waits behind another logger. A
    terminal event is ineligible while its accepted predecessor is pending, including when
    another worker owns that predecessor's lock.
    """
    delivered = 0
    async with async_session_maker() as db:
        accepted_predecessor = aliased(InstanceOperationAuditOutbox)
        query = (
            select(InstanceOperationAuditOutbox, InstanceOperationJob)
            .join(
                InstanceOperationJob,
                InstanceOperationJob.job_id == InstanceOperationAuditOutbox.job_id,
            )
            .where(
                InstanceOperationAuditOutbox.delivered_at.is_(None),
                or_(
                    InstanceOperationAuditOutbox.stage == "accepted",
                    ~exists(
                        select(accepted_predecessor.id).where(
                            accepted_predecessor.job_id
                            == InstanceOperationAuditOutbox.job_id,
                            accepted_predecessor.stage == "accepted",
                            accepted_predecessor.delivered_at.is_(None),
                        )
                    ),
                ),
            )
            .order_by(
                InstanceOperationAuditOutbox.created_at,
                InstanceOperationAuditOutbox.id,
            )
            .limit(max(1, min(limit, 500)))
            .with_for_update(skip_locked=True, of=InstanceOperationAuditOutbox)
        )
        if job_id is not None:
            query = query.where(InstanceOperationAuditOutbox.job_id == job_id)
        rows = (await db.execute(query)).all()
        blocked_jobs: set[str] = set()
        for event, job in rows:
            if event.stage == "terminal" and event.job_id in blocked_jobs:
                continue
            try:
                get_activity_logger().log_activity(**_audit_values(event, job))
            except Exception as exc:  # noqa: BLE001 - leave the event pending for replay
                event.attempts = int(event.attempts or 0) + 1
                event.last_error = sanitize_error(str(exc), limit=500)
                logger.warning(
                    "Could not deliver instance operation audit %s: %s",
                    event.event_id,
                    event.last_error,
                )
                if event.stage == "accepted":
                    blocked_jobs.add(event.job_id)
            else:
                event.attempts = int(event.attempts or 0) + 1
                event.last_error = None
                event.delivered_at = datetime.now(timezone.utc)
                delivered += 1
        await db.commit()
    return delivered
