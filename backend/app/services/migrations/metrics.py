"""Low-cardinality operational aggregates for the Migration Center."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import case, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.migration_models import (
    MigrationAuditOutbox,
    MigrationJob,
    MigrationResource,
    MigrationResourceLock,
)
from app.models.migration_schemas import (
    MigrationAuthorizationDenialMetrics,
    MigrationCleanupOverdueMetrics,
    MigrationJobPhase,
    MigrationOperationalMetricsResponse,
    MigrationOrphanDetectionMetrics,
    MigrationPhaseDurationAggregate,
    MigrationReconciliationMetrics,
    MigrationResourceState,
    MigrationRollbackMetrics,
    MigrationTransferMetrics,
    MigrationUnavailableMetric,
)
from app.models.task_journal_models import ClusterLog

from .repository import TERMINAL_STATUSES, utcnow


_OUTCOME_BY_STATUS = {
    "pending": "in_progress",
    "running": "in_progress",
    "waiting": "awaiting_operator",
    "success": "success",
    "failure": "failure",
    "cancelled": "cancelled",
    "unknown": "unknown",
    "manual_recovery": "manual_recovery",
}
_OUTCOMES = tuple(dict.fromkeys(_OUTCOME_BY_STATUS.values())) + ("unrecognized",)
_CLEANUP_PENDING_PHASES = (
    "source_retained",
    "cleanup_due",
    "cleanup_blocked",
    "cleaning_up",
)


def _count_rows(rows: list[Any], allowed: tuple[str, ...]) -> dict[str, int]:
    counts = {value: 0 for value in allowed}
    counts["unrecognized"] = 0
    for raw_name, raw_count in rows:
        name = str(raw_name)
        count = int(raw_count or 0)
        if name in counts and name != "unrecognized":
            counts[name] += count
        else:
            counts["unrecognized"] += count
    return counts


def _duration_statement(as_of: datetime):
    """Aggregate contiguous durable phase visits without exposing job identifiers."""
    phase_value = MigrationAuditOutbox.payload["phase"].astext
    raw_events = (
        select(
            MigrationAuditOutbox.job_id.label("job_id"),
            phase_value.label("phase"),
            MigrationAuditOutbox.sequence.label("sequence"),
            MigrationAuditOutbox.created_at.label("entered_at"),
            func.lag(phase_value)
            .over(
                partition_by=MigrationAuditOutbox.job_id,
                order_by=MigrationAuditOutbox.sequence,
            )
            .label("previous_phase"),
        )
        .where(
            MigrationAuditOutbox.channel == "audit",
            phase_value.in_(tuple(phase.value for phase in MigrationJobPhase)),
        )
        .subquery()
    )
    phase_entries = (
        select(
            raw_events.c.job_id,
            raw_events.c.phase,
            raw_events.c.sequence,
            raw_events.c.entered_at,
        )
        .where(
            or_(
                raw_events.c.previous_phase.is_(None),
                raw_events.c.previous_phase != raw_events.c.phase,
            )
        )
        .subquery()
    )
    phase_spans = (
        select(
            phase_entries.c.job_id,
            phase_entries.c.phase,
            phase_entries.c.sequence,
            phase_entries.c.entered_at,
            func.lead(phase_entries.c.entered_at)
            .over(
                partition_by=phase_entries.c.job_id,
                order_by=phase_entries.c.sequence,
            )
            .label("next_entered_at"),
        )
        .subquery()
    )
    terminal_end = case(
        (
            MigrationJob.finished_at.is_not(None),
            func.greatest(MigrationJob.finished_at, phase_spans.c.entered_at),
        ),
        else_=literal(as_of),
    )
    duration_seconds = func.greatest(
        literal(0.0),
        func.extract(
            "epoch",
            func.coalesce(phase_spans.c.next_entered_at, terminal_end)
            - phase_spans.c.entered_at,
        ),
    )
    return (
        select(
            phase_spans.c.phase,
            func.count().label("sample_count"),
            func.avg(duration_seconds).label("average_seconds"),
            func.max(duration_seconds).label("maximum_seconds"),
        )
        .join(MigrationJob, MigrationJob.job_id == phase_spans.c.job_id)
        .group_by(phase_spans.c.phase)
    )


async def collect_migration_metrics(
    db: AsyncSession,
) -> MigrationOperationalMetricsResponse:
    """Read aggregate-only telemetry from durable control-plane tables."""
    as_of = utcnow()
    phase_values = tuple(phase.value for phase in MigrationJobPhase)
    resource_states = tuple(state.value for state in MigrationResourceState)

    phase_rows = list(
        (
            await db.execute(
                select(MigrationJob.phase, func.count())
                .select_from(MigrationJob)
                .group_by(MigrationJob.phase)
            )
        ).all()
    )
    status_rows = list(
        (
            await db.execute(
                select(MigrationJob.status, func.count())
                .select_from(MigrationJob)
                .group_by(MigrationJob.status)
            )
        ).all()
    )
    resource_rows = list(
        (
            await db.execute(
                select(MigrationResource.state, func.count())
                .select_from(MigrationResource)
                .group_by(MigrationResource.state)
            )
        ).all()
    )
    duration_rows = list((await db.execute(_duration_statement(as_of))).all())

    reconciliation_phase = MigrationAuditOutbox.payload["phase"].astext
    reconciliation_events, reconciliation_jobs = (
        await db.execute(
            select(
                func.count(MigrationAuditOutbox.id),
                func.count(func.distinct(MigrationAuditOutbox.job_id)),
            ).where(
                MigrationAuditOutbox.channel == "audit",
                reconciliation_phase == "reconciling",
            )
        )
    ).one()

    completed_rollbacks, move_jobs = (
        await db.execute(
            select(
                func.count().filter(MigrationJob.phase == "rolled_back"),
                func.count().filter(MigrationJob.intent == "move"),
            ).select_from(MigrationJob)
        )
    ).one()

    overdue_predicate = (
        (MigrationJob.intent == "move")
        & MigrationJob.retention_deadline.is_not(None)
        & (MigrationJob.retention_deadline <= as_of)
        & MigrationJob.phase.in_(_CLEANUP_PENDING_PHASES)
    )
    overdue_jobs, blocked_cleanup_jobs = (
        await db.execute(
            select(
                func.count().filter(overdue_predicate),
                func.count().filter(
                    overdue_predicate & (MigrationJob.phase == "cleanup_blocked")
                ),
            ).select_from(MigrationJob)
        )
    ).one()

    authorization_denials = int(
        (
            await db.execute(
                select(func.count(ClusterLog.id)).where(
                    ClusterLog.action == "rbac.access_denied",
                    ClusterLog.result == "denied",
                    ClusterLog.resource_name.like("%/migrations%"),
                )
            )
        ).scalar_one()
        or 0
    )

    orphaned_locks = int(
        (
            await db.execute(
                select(func.count(MigrationResourceLock.id))
                .select_from(MigrationResourceLock)
                .outerjoin(
                    MigrationJob,
                    MigrationJob.job_id == MigrationResourceLock.job_id,
                )
                .where(
                    or_(
                        MigrationJob.id.is_(None),
                        MigrationJob.status.in_(TERMINAL_STATUSES),
                    )
                )
            )
        ).scalar_one()
        or 0
    )

    jobs_by_phase = _count_rows(phase_rows, phase_values)
    status_counts = _count_rows(status_rows, tuple(_OUTCOME_BY_STATUS))
    jobs_by_outcome = {outcome: 0 for outcome in _OUTCOMES}
    for status_name, count in status_counts.items():
        outcome = _OUTCOME_BY_STATUS.get(status_name, "unrecognized")
        jobs_by_outcome[outcome] += count

    phase_durations = {
        phase: MigrationPhaseDurationAggregate(
            sample_count=0,
            average_seconds=None,
            maximum_seconds=None,
        )
        for phase in phase_values
    }
    for phase, sample_count, average_seconds, maximum_seconds in duration_rows:
        phase_name = str(phase)
        if phase_name not in phase_durations:
            continue
        phase_durations[phase_name] = MigrationPhaseDurationAggregate(
            sample_count=int(sample_count or 0),
            average_seconds=(
                max(0.0, float(average_seconds))
                if average_seconds is not None
                else None
            ),
            maximum_seconds=(
                max(0.0, float(maximum_seconds))
                if maximum_seconds is not None
                else None
            ),
        )

    completed_rollbacks = int(completed_rollbacks or 0)
    move_jobs = int(move_jobs or 0)
    return MigrationOperationalMetricsResponse(
        generated_at=as_of,
        jobs_total=sum(jobs_by_outcome.values()),
        jobs_by_phase=jobs_by_phase,
        jobs_by_outcome=jobs_by_outcome,
        phase_duration_seconds=phase_durations,
        transfer_progress=MigrationTransferMetrics(
            resources_total=sum(int(count or 0) for _, count in resource_rows),
            resources_by_state=_count_rows(resource_rows, resource_states),
            byte_progress=MigrationUnavailableMetric(
                reason=(
                    "The durable engine records indeterminate transfer progress only; "
                    "byte counters are not persisted."
                )
            ),
        ),
        reconciliation=MigrationReconciliationMetrics(
            events_total=int(reconciliation_events or 0),
            jobs_total=int(reconciliation_jobs or 0),
        ),
        rollback=MigrationRollbackMetrics(
            completed_jobs=completed_rollbacks,
            move_jobs=move_jobs,
            completion_rate=(completed_rollbacks / move_jobs if move_jobs else 0.0),
        ),
        cleanup_overdue=MigrationCleanupOverdueMetrics(
            jobs_total=int(overdue_jobs or 0),
            blocked_jobs=int(blocked_cleanup_jobs or 0),
        ),
        authorization_denials=MigrationAuthorizationDenialMetrics(
            denied_requests_total=authorization_denials,
            coverage=(
                "Counts durable Migration Center RBAC endpoint denials. Project-scope and "
                "current-authority refusals do not yet have a dedicated durable counter."
            ),
        ),
        orphan_detection=MigrationOrphanDetectionMetrics(
            orphaned_lock_rows=orphaned_locks,
            daemon_resources=MigrationUnavailableMetric(
                reason=(
                    "Daemon orphan resources are not recorded as a durable aggregate; "
                    "the reported count covers stale control-plane lock rows only."
                )
            ),
        ),
    )
