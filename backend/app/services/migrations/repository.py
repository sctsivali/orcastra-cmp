"""Transactional persistence for migration plans, jobs, resources, and leases."""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import (
    String,
    and_,
    case,
    cast,
    delete,
    exists,
    false,
    func,
    literal,
    or_,
    select,
    text,
    true,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.db_models import (
    Organization,
    OrganizationCluster,
    OrganizationMember,
    TenantAccessPolicy,
    TenantPolicyRule,
)
from app.models.migration_models import (
    MigrationAuditOutbox,
    MigrationJob,
    MigrationPlan,
    MigrationResource,
    MigrationResourceLock,
)
from app.core.config import get_settings

from .security import canonical_resource_key, sanitize_error, sanitize_payload

LEASE_DURATION = timedelta(seconds=90)
ACTIVE_STATUSES = ("pending", "running", "waiting", "unknown", "manual_recovery")
CLAIMABLE_STATUSES = ("pending", "running", "unknown")
TERMINAL_STATUSES = ("success", "failure", "cancelled")
_CLAIM_ADVISORY_KEY = 5_178_261_933


@dataclass(frozen=True)
class MigrationListVisibility:
    """Immutable request authority, applied as a filter before pagination.

    ``visible_projects`` contains only clusters with a project boundary. A cluster absent
    from that tuple is unrestricted by project, matching the request-scoped organization
    boundary in ``org_scope``.
    """

    role: str
    actor_issuer: str
    actor_subject: str
    actor_email: str
    actor_provider_uuid: str
    allowed_clusters: Optional[frozenset[str]]
    visible_projects: Optional[tuple[tuple[str, frozenset[str]], ...]]


class RepositoryConflict(RuntimeError):
    pass


_EVENT_ACTOR_FIELDS = (
    "actor_issuer",
    "actor_subject",
    "actor_email",
    "actor_provider_uuid",
    "requester_role",
)


def event_actor(values: Mapping[str, Any]) -> dict[str, str]:
    """Return the immutable, allowlisted actor snapshot stored with one event."""
    missing = [
        field
        for field in _EVENT_ACTOR_FIELDS
        if not isinstance(values.get(field), str) or not str(values[field]).strip()
    ]
    if missing:
        raise RepositoryConflict("Migration audit actor identity is incomplete.")
    role = str(values["requester_role"])
    if role not in {"admin", "partner"}:
        raise RepositoryConflict("Migration audit actor role is invalid.")
    return {
        "issuer": str(values["actor_issuer"]),
        "subject": str(values["actor_subject"]),
        "email": str(values["actor_email"]),
        "provider_uuid": str(values["actor_provider_uuid"]),
        "role": role,
    }


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _holds_concurrency_slot(_now: datetime):
    """Reserve capacity until a job reaches an authoritative non-active state.

    A worker lease proves who may mutate; it does not prove whether the daemon stopped working.
    A crashed worker can leave an accepted operation running after its SQL lease expires, so every
    ``running`` job keeps its slot while recovery reclaims that same job.
    """
    return or_(
        MigrationJob.status == "running",
        MigrationJob.status.in_(("unknown", "manual_recovery")),
        MigrationJob.phase == "reconciling",
    )


def _request_endpoint_visibility(
    cluster_column: Any,
    project_column: Any,
    visibility: MigrationListVisibility,
) -> Any:
    predicates: list[Any] = []
    if visibility.allowed_clusters is not None:
        if not visibility.allowed_clusters:
            return false()
        predicates.append(cluster_column.in_(visibility.allowed_clusters))

    if visibility.visible_projects:
        bounded_clusters = tuple(cluster for cluster, _projects in visibility.visible_projects)
        allowed_pairs: list[Any] = []
        for cluster, projects in visibility.visible_projects:
            if projects:
                allowed_pairs.append(and_(cluster_column == cluster, project_column.in_(projects)))
        predicates.append(or_(cluster_column.not_in(bounded_clusters), *allowed_pairs))

    return and_(*predicates) if predicates else literal(True)


def _shared_cluster_ids() -> Any:
    owner = aliased(OrganizationCluster)
    owner_key = case(
        (
            owner.organization_id.is_(None),
            literal("unbound:") + cast(owner.id, String),
        ),
        else_=literal("org:") + cast(owner.organization_id, String),
    )
    return select(owner.cluster_id).group_by(owner.cluster_id).having(func.count(func.distinct(owner_key)) > 1)


def _partner_endpoint_authority(
    model: Any,
    cluster_column: Any,
    project_column: Any,
) -> Any:
    binding = aliased(OrganizationCluster)
    policy = aliased(TenantAccessPolicy)
    rule = aliased(TenantPolicyRule)

    binding_exists = exists(
        select(1)
        .select_from(binding)
        .where(
            binding.organization_id == model.source_organization_id,
            binding.cluster_id == cluster_column,
        )
    )
    project_grant_exists = exists(
        select(1)
        .select_from(rule)
        .join(policy, policy.id == rule.policy_id)
        .where(
            policy.organization_id == model.source_organization_id,
            rule.cluster_id == cluster_column,
            rule.project_name == project_column,
        )
    )
    return and_(
        binding_exists,
        or_(
            project_column == "default",
            cluster_column.not_in(_shared_cluster_ids()),
            project_grant_exists,
        ),
    )


def _partner_current_authority(
    model: Any,
    visibility: MigrationListVisibility,
) -> Any:
    organization = aliased(Organization)
    member = aliased(OrganizationMember)
    member_exists = exists(
        select(1)
        .select_from(member)
        .where(
            member.organization_id == organization.id,
            member.user_email == visibility.actor_email,
        )
    )
    affiliated = exists(
        select(1)
        .select_from(organization)
        .where(
            organization.id == model.source_organization_id,
            organization.is_active.is_(True),
            or_(
                organization.created_by == visibility.actor_email,
                member_exists,
            ),
        )
    )
    return and_(
        affiliated,
        _partner_endpoint_authority(
            model,
            model.source_cluster_id,
            model.source_project,
        ),
        _partner_endpoint_authority(
            model,
            model.target_cluster_id,
            model.target_project,
        ),
    )


def _list_visibility_predicate(
    model: Any,
    visibility: MigrationListVisibility,
    *,
    exact_actor: bool,
) -> Any:
    if visibility.role not in {"admin", "partner"}:
        return false()

    predicates = [
        model.source_organization_id == model.target_organization_id,
        _request_endpoint_visibility(
            model.source_cluster_id,
            model.source_project,
            visibility,
        ),
        _request_endpoint_visibility(
            model.target_cluster_id,
            model.target_project,
            visibility,
        ),
    ]
    if exact_actor:
        predicates.extend(
            (
                model.requester == visibility.actor_email,
                model.actor_issuer == visibility.actor_issuer,
                model.actor_subject == visibility.actor_subject,
                model.actor_email == visibility.actor_email,
                model.actor_provider_uuid == visibility.actor_provider_uuid,
            )
        )
    if visibility.role == "partner":
        predicates.append(_partner_current_authority(model, visibility))
    return and_(*predicates)


async def _list_visible_page(
    db: AsyncSession,
    model: Any,
    predicate: Any,
    *,
    sort_column: Any,
    limit: int,
    offset: int,
) -> tuple[list[Any], int]:
    """Return one page and its exact total from one database snapshot."""
    visible = (
        select(
            model.id.label("row_id"),
            sort_column.label("sort_timestamp"),
        )
        .where(predicate)
        .cte(f"visible_{model.__tablename__}")
    )
    page = (
        select(visible.c.row_id, visible.c.sort_timestamp)
        .order_by(visible.c.sort_timestamp.desc(), visible.c.row_id.desc())
        .offset(offset)
        .limit(limit)
        .cte(f"page_{model.__tablename__}")
    )
    total = select(func.count().label("visible_total")).select_from(visible).cte(f"total_{model.__tablename__}")
    statement = (
        select(model, total.c.visible_total)
        .select_from(
            total.outerjoin(page, true()).outerjoin(
                model,
                model.id == page.c.row_id,
            )
        )
        .order_by(page.c.sort_timestamp.desc(), page.c.row_id.desc())
    )
    records = (await db.execute(statement)).all()
    exact_total = int(records[0][1] or 0) if records else 0
    return [record[0] for record in records if record[0] is not None], exact_total


async def get_plan(db: AsyncSession, plan_id: str, *, lock: bool = False) -> Optional[MigrationPlan]:
    stmt = select(MigrationPlan).where(MigrationPlan.plan_id == plan_id)
    if lock:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_plans(
    db: AsyncSession,
    visibility: MigrationListVisibility,
    *,
    limit: int,
    offset: int,
) -> tuple[list[MigrationPlan], int]:
    predicate = _list_visibility_predicate(
        MigrationPlan,
        visibility,
        exact_actor=True,
    )
    rows, total = await _list_visible_page(
        db,
        MigrationPlan,
        predicate,
        sort_column=MigrationPlan.updated_at,
        limit=limit,
        offset=offset,
    )
    return list(rows), total


async def insert_plan(db: AsyncSession, **values: Any) -> MigrationPlan:
    now = utcnow()
    ttl_seconds = max(60, int(get_settings().migration_plan_ttl_seconds))
    row = MigrationPlan(
        plan_id=secrets.token_hex(16),
        revision=1,
        expires_at=now + timedelta(seconds=ttl_seconds),
        created_at=now,
        updated_at=now,
        **values,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def replace_plan(
    db: AsyncSession,
    row: MigrationPlan,
    *,
    expected_revision: int,
    values: dict[str, Any],
) -> MigrationPlan:
    if row.revision != expected_revision:
        raise RepositoryConflict("The plan changed. Reload it before applying changes.")
    if row.status == "executed":
        raise RepositoryConflict("An executed plan cannot be changed.")
    now = utcnow()
    ttl_seconds = max(60, int(get_settings().migration_plan_ttl_seconds))
    for key, value in values.items():
        setattr(row, key, value)
    row.revision += 1
    row.expires_at = now + timedelta(seconds=ttl_seconds)
    row.updated_at = now
    await db.commit()
    await db.refresh(row)
    return row


async def revise_plan_preflight(
    db: AsyncSession,
    row: MigrationPlan,
    *,
    report: dict[str, Any],
) -> MigrationPlan:
    """Persist fresh approval evidence as a new revision without accepting execution."""
    now = utcnow()
    ttl_seconds = max(60, int(get_settings().migration_plan_ttl_seconds))
    # Stored as given, like insert_plan and replace_plan. The approval hash covers this exact
    # report; a sanitized copy would differ, and every later execute would ask to re-approve.
    row.preflight_payload = report
    row.status = "ready" if not report.get("blockers") else "blocked"
    row.revision += 1
    row.expires_at = now + timedelta(seconds=ttl_seconds)
    row.updated_at = now
    await db.commit()
    await db.refresh(row)
    return row


async def get_job(db: AsyncSession, job_id: str, *, lock: bool = False) -> Optional[MigrationJob]:
    stmt = select(MigrationJob).where(MigrationJob.job_id == job_id)
    if lock:
        # Without populate_existing, a row this session already read keeps its stale attributes
        # and a revision check made under the lock would compare against the earlier read.
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return (await db.execute(stmt)).scalar_one_or_none()


async def get_job_by_request(db: AsyncSession, requester: str, idempotency_key: str) -> Optional[MigrationJob]:
    return (
        await db.execute(
            select(MigrationJob).where(
                MigrationJob.requester == requester,
                MigrationJob.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()


# The console's tabs (frontend migration-format.ts migrationInView), decided on the server so a
# view is never limited to the jobs one page of the whole list happened to hold.
_HISTORY_PHASES = ("completed", "rolled_back", "cancelled", "failed")
_ATTENTION_PHASES = (
    "cutover_pending", "external_cutover_pending", "unknown", "reconciling", "rollback_available",
    "cleanup_due", "cleanup_blocked", "manual_recovery", "failed",
)
JOB_VIEWS = ("active", "attention", "history")


def _view_predicate(view: str) -> Any:
    history = or_(
        MigrationJob.phase.in_(_HISTORY_PHASES),
        MigrationJob.status.in_(("success", "failure", "cancelled")),
    )
    if view == "history":
        return history
    if view == "attention":
        return or_(
            MigrationJob.phase.in_(_ATTENTION_PHASES),
            MigrationJob.status.in_(("failure", "unknown", "manual_recovery")),
        )
    return ~history


async def count_jobs_by_view(db: AsyncSession, *, visibility: MigrationListVisibility) -> dict[str, int]:
    visible = _list_visibility_predicate(MigrationJob, visibility, exact_actor=False)
    counts: dict[str, int] = {}
    for view in JOB_VIEWS:
        counts[view] = int(
            (await db.execute(
                select(func.count()).select_from(MigrationJob).where(visible, _view_predicate(view))
            )).scalar()
            or 0
        )
    return counts


async def list_jobs(
    db: AsyncSession,
    *,
    visibility: MigrationListVisibility,
    limit: int,
    offset: int,
    view: Optional[str] = None,
) -> tuple[list[MigrationJob], int]:
    predicate = _list_visibility_predicate(
        MigrationJob,
        visibility,
        exact_actor=False,
    )
    if view is not None:
        predicate = and_(predicate, _view_predicate(view))
    rows, total = await _list_visible_page(
        db,
        MigrationJob,
        predicate,
        sort_column=MigrationJob.created_at,
        limit=limit,
        offset=offset,
    )
    return list(rows), total


async def list_resources(db: AsyncSession, job_id: str) -> list[MigrationResource]:
    return list(
        (
            await db.execute(
                select(MigrationResource).where(MigrationResource.job_id == job_id).order_by(MigrationResource.ordinal)
            )
        )
        .scalars()
        .all()
    )


async def list_timeline(db: AsyncSession, job_id: str) -> list[MigrationAuditOutbox]:
    return list(
        (
            await db.execute(
                select(MigrationAuditOutbox)
                .where(
                    MigrationAuditOutbox.job_id == job_id,
                    MigrationAuditOutbox.channel == "audit",
                )
                .order_by(MigrationAuditOutbox.sequence)
            )
        )
        .scalars()
        .all()
    )


async def _next_sequence(db: AsyncSession, job_id: str, channel: str) -> int:
    value = (
        await db.execute(
            select(func.max(MigrationAuditOutbox.sequence)).where(
                MigrationAuditOutbox.job_id == job_id,
                MigrationAuditOutbox.channel == channel,
            )
        )
    ).scalar()
    return int(value) + 1 if value is not None else 0


async def enqueue_event(
    db: AsyncSession,
    *,
    job_id: str,
    event_type: str,
    phase: str,
    message: str,
    actor: Mapping[str, str],
    channel: str = "audit",
    extra: Optional[dict[str, Any]] = None,
    event_id: Optional[str] = None,
    reason: Optional[str] = None,
) -> None:
    if event_id is not None:
        existing = (
            await db.execute(select(MigrationAuditOutbox.id).where(MigrationAuditOutbox.event_id == event_id))
        ).scalar_one_or_none()
        if existing is not None:
            return
    sequence = await _next_sequence(db, job_id, channel)
    payload = sanitize_payload(
        {
            "phase": phase,
            "message": message,
            "actor": dict(actor),
            **({"reason": reason} if reason is not None else {}),
            **(extra or {}),
        }
    )
    db.add(
        MigrationAuditOutbox(
            event_id=event_id or secrets.token_hex(16),
            job_id=job_id,
            channel=channel,
            event_type=event_type,
            sequence=sequence,
            payload=payload,
            status="pending",
        )
    )
    # important: sessions run with autoflush off, so the next event on this channel in the same
    # transaction would read the same max sequence and collide (ex; a project emits one webhook
    # per instance in its retention transaction).
    await db.flush()


async def create_job(
    db: AsyncSession,
    *,
    plan: MigrationPlan,
    requester: str,
    requester_role: str,
    actor_issuer: str,
    actor_subject: str,
    actor_email: str,
    actor_provider_uuid: str,
    idempotency_key: str,
    payload_hash: str,
    reason: str,
    acknowledgements: list[str],
) -> tuple[MigrationJob, bool]:
    existing = await get_job_by_request(db, requester, idempotency_key)
    if existing is not None:
        if existing.payload_hash != payload_hash:
            raise RepositoryConflict("This Idempotency-Key was already used for a different execution request.")
        return existing, False
    now = utcnow()
    preflight = plan.preflight_payload or {}
    row = MigrationJob(
        job_id=secrets.token_hex(16),
        plan_id=plan.plan_id,
        plan_revision=plan.revision,
        revision=1,
        scope=plan.scope,
        intent=plan.intent,
        requester=requester,
        requester_role=requester_role,
        actor_issuer=actor_issuer,
        actor_subject=actor_subject,
        actor_email=actor_email,
        actor_provider_uuid=actor_provider_uuid,
        source_organization_id=plan.source_organization_id,
        target_organization_id=plan.target_organization_id,
        source_cluster_id=plan.source_cluster_id,
        source_project=plan.source_project,
        source_resource_name=plan.source_resource_name,
        source_member=plan.source_member,
        source_cluster_fingerprint=plan.source_cluster_fingerprint,
        target_cluster_id=plan.target_cluster_id,
        target_project=plan.target_project,
        target_resource_name=plan.target_resource_name,
        target_member=plan.target_member,
        target_cluster_fingerprint=plan.target_cluster_fingerprint,
        source_key=plan.source_key,
        target_key=plan.target_key,
        active_source_key=plan.source_key,
        active_target_key=plan.target_key,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
        plan_payload_hash=plan.payload_hash,
        strategy=str(preflight.get("strategy") or "unknown"),
        downtime_class=str(preflight.get("downtime_class") or "unknown"),
        phase="queued",
        status="pending",
        retention_hours=plan.retention_hours,
        external_cutover_required=plan.external_cutover_required,
        safe_result=sanitize_payload({"reason": reason, "acknowledgements": acknowledgements}),
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    try:
        await db.flush()
        lock_specs = {
            (
                canonical_resource_key("project", plan.source_cluster_id, plan.source_project, None),
                "project",
                plan.source_cluster_id,
                plan.source_project,
                None,
            ),
            (
                canonical_resource_key("project", plan.target_cluster_id, plan.target_project, None),
                "project",
                plan.target_cluster_id,
                plan.target_project,
                None,
            ),
        }
        if plan.scope == "instance":
            source_name = str(plan.source_resource_name)
            target_name = str(plan.target_resource_name or plan.source_resource_name)
            lock_specs.update(
                {
                    (
                        canonical_resource_key(
                            "instance",
                            plan.source_cluster_id,
                            plan.source_project,
                            source_name,
                        ),
                        "instance",
                        plan.source_cluster_id,
                        plan.source_project,
                        source_name,
                    ),
                    (
                        canonical_resource_key(
                            "instance",
                            plan.target_cluster_id,
                            plan.target_project,
                            target_name,
                        ),
                        "instance",
                        plan.target_cluster_id,
                        plan.target_project,
                        target_name,
                    ),
                }
            )
        for lock_key, lock_scope, cluster_id, project, resource_name in sorted(lock_specs, key=lambda spec: spec[0]):
            db.add(
                MigrationResourceLock(
                    job_id=row.job_id,
                    lock_key=lock_key,
                    lock_scope=lock_scope,
                    cluster_id=cluster_id,
                    project=project,
                    resource_name=resource_name,
                )
            )
        for ordinal, resource in enumerate(preflight.get("resources") or []):
            source = resource.get("source") or {"name": resource.get("source_name")}
            target = resource.get("target") or {"name": resource.get("target_name")}
            resource_key = str(
                resource.get("resource_key")
                or f"{resource.get('resource_type', 'resource')}:{source.get('name') or ordinal}"
            )
            db.add(
                MigrationResource(
                    resource_id=secrets.token_hex(16),
                    job_id=row.job_id,
                    resource_key=resource_key,
                    resource_type=str(resource.get("resource_type") or "resource"),
                    ordinal=ordinal,
                    state="pending",
                    source_ref=sanitize_payload(source),
                    target_ref=sanitize_payload(target),
                    dependency_keys=list(resource.get("dependency_keys") or []),
                    progress={"mode": "indeterminate"},
                )
            )
        await enqueue_event(
            db,
            job_id=row.job_id,
            event_type="migration.accepted",
            phase="queued",
            message="Migration was accepted and queued.",
            actor=event_actor(
                {
                    "requester_role": requester_role,
                    "actor_issuer": actor_issuer,
                    "actor_subject": actor_subject,
                    "actor_email": actor_email,
                    "actor_provider_uuid": actor_provider_uuid,
                }
            ),
            reason=reason,
        )
        plan.status = "executed"
        plan.updated_at = now
        await db.commit()
    except IntegrityError:
        await db.rollback()
        winner = await get_job_by_request(db, requester, idempotency_key)
        if winner is not None and winner.payload_hash == payload_hash:
            return winner, False
        raise RepositoryConflict("Another migration for this source or target is already active.") from None
    await db.refresh(row)
    return row, True


async def set_job(
    db: AsyncSession,
    job_id: str,
    *,
    worker_id: Optional[str] = None,
    fence_token: Optional[int] = None,
    event_message: Optional[str] = None,
    event_type: str = "migration.phase_changed",
    event_actor: Optional[Mapping[str, str]] = None,
    event_reason: Optional[str] = None,
    terminal: bool = False,
    outbox_events: Optional[list[dict[str, Any]]] = None,
    expected_revision: Optional[int] = None,
    **values: Any,
) -> bool:
    now = utcnow()
    if (event_message or outbox_events) and event_actor is None:
        raise RepositoryConflict("Migration event actor is required.")
    predicates = [MigrationJob.job_id == job_id]
    if expected_revision is not None:
        # Compare and set: an operator action applies only to the revision it was checked against.
        predicates.append(MigrationJob.revision == expected_revision)
    if worker_id is not None:
        predicates.extend(
            (
                MigrationJob.locked_by == worker_id,
                MigrationJob.status == "running",
                MigrationJob.lease_expires_at > now,
            )
        )
    if fence_token is not None:
        predicates.append(MigrationJob.fence_token == fence_token)
    if terminal:
        values.setdefault("active_source_key", None)
        values.setdefault("active_target_key", None)
        values.setdefault("finished_at", now)
        values.setdefault("locked_by", None)
        values.setdefault("lease_expires_at", None)
    values["updated_at"] = now
    values["revision"] = MigrationJob.revision + 1
    result = await db.execute(update(MigrationJob).where(*predicates).values(**values))
    if result.rowcount and terminal:
        await db.execute(delete(MigrationResourceLock).where(MigrationResourceLock.job_id == job_id))
    if result.rowcount and event_message:
        phase = str(values.get("phase") or "queued")
        await enqueue_event(
            db,
            job_id=job_id,
            event_type=event_type,
            phase=phase,
            message=event_message,
            actor=event_actor,
            reason=event_reason,
        )
    if result.rowcount:
        for event in outbox_events or []:
            await enqueue_event(db, job_id=job_id, actor=event_actor, **event)
    await db.commit()
    return bool(result.rowcount)


async def set_resource(
    db: AsyncSession,
    resource_id: str,
    *,
    job_id: str,
    worker_id: str,
    fence_token: int,
    state: str,
    operation_ref: Optional[dict[str, Any]] = None,
    safe_result: Optional[dict[str, Any]] = None,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
    progress: Optional[dict[str, Any]] = None,
) -> None:
    lease = (
        await db.execute(
            select(MigrationJob)
            .where(
                MigrationJob.job_id == job_id,
                MigrationJob.locked_by == worker_id,
                MigrationJob.fence_token == fence_token,
                MigrationJob.status == "running",
                MigrationJob.lease_expires_at > utcnow(),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if lease is None:
        raise RepositoryConflict("Migration lease ownership was lost.")
    row = (
        await db.execute(
            select(MigrationResource)
            .where(
                MigrationResource.resource_id == resource_id,
                MigrationResource.job_id == job_id,
            )
            .with_for_update()
        )
    ).scalar_one()
    if operation_ref:
        refs = list(row.operation_refs or [])
        if operation_ref not in refs:
            refs.append(sanitize_payload(operation_ref))
        row.operation_refs = refs
    row.state = state
    if safe_result is not None:
        row.safe_result = sanitize_payload(safe_result)
    if progress is not None:
        row.progress = sanitize_payload(progress)
    row.error_code = error_code
    row.error_message = sanitize_error(error_message) if error_message else None
    row.updated_at = utcnow()
    if state in {"completed", "failed", "skipped"}:
        row.finished_at = utcnow()
    await db.commit()


async def assert_lease(db: AsyncSession, job_id: str, worker_id: str, fence_token: int) -> bool:
    """Confirm the current worker still owns a non-expired mutation lease."""
    row = (
        await db.execute(
            select(MigrationJob.job_id).where(
                MigrationJob.job_id == job_id,
                MigrationJob.locked_by == worker_id,
                MigrationJob.fence_token == fence_token,
                MigrationJob.status == "running",
                MigrationJob.lease_expires_at > utcnow(),
            )
        )
    ).first()
    return row is not None


async def claim_job(db: AsyncSession, job_id: str, worker_id: str) -> Optional[MigrationJob]:
    now = utcnow()
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _CLAIM_ADVISORY_KEY})
    row = (
        await db.execute(
            select(MigrationJob)
            .where(
                MigrationJob.job_id == job_id,
                MigrationJob.status.in_(CLAIMABLE_STATUSES),
                or_(
                    MigrationJob.lease_expires_at.is_(None),
                    MigrationJob.lease_expires_at < now,
                ),
            )
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if row is None:
        await db.rollback()
        return None
    busy_global = int(
        (
            await db.execute(
                select(func.count())
                .select_from(MigrationJob)
                .where(
                    _holds_concurrency_slot(now),
                    MigrationJob.job_id != job_id,
                )
            )
        ).scalar()
        or 0
    )
    busy_org = int(
        (
            await db.execute(
                select(func.count())
                .select_from(MigrationJob)
                .where(
                    _holds_concurrency_slot(now),
                    MigrationJob.source_organization_id == row.source_organization_id,
                    MigrationJob.job_id != job_id,
                )
            )
        ).scalar()
        or 0
    )
    cluster_conflict = (
        await db.execute(
            select(MigrationJob.job_id)
            .where(
                _holds_concurrency_slot(now),
                MigrationJob.job_id != job_id,
                or_(
                    MigrationJob.source_cluster_id.in_([row.source_cluster_id, row.target_cluster_id]),
                    MigrationJob.target_cluster_id.in_([row.source_cluster_id, row.target_cluster_id]),
                ),
            )
            .limit(1)
        )
    ).first()
    global_limit = max(1, int(get_settings().migration_global_concurrency))
    if busy_global >= global_limit or busy_org >= 1 or cluster_conflict is not None:
        await db.rollback()
        return None
    row.locked_by = worker_id
    row.lease_expires_at = now + LEASE_DURATION
    row.heartbeat_at = now
    row.fence_token += 1
    row.attempt_count += 1
    row.status = "running"
    row.started_at = row.started_at or now
    row.updated_at = now
    await db.commit()
    await db.refresh(row)
    return row


async def heartbeat(db: AsyncSession, job_id: str, worker_id: str, fence_token: int) -> bool:
    now = utcnow()
    result = await db.execute(
        update(MigrationJob)
        .where(
            MigrationJob.job_id == job_id,
            MigrationJob.locked_by == worker_id,
            MigrationJob.fence_token == fence_token,
            MigrationJob.status == "running",
            MigrationJob.lease_expires_at > now,
        )
        .values(
            lease_expires_at=now + LEASE_DURATION,
            heartbeat_at=now,
            updated_at=now,
        )
    )
    await db.commit()
    return bool(result.rowcount)


async def recoverable_job_ids(db: AsyncSession, *, limit: int = 50) -> list[str]:
    """Claimable jobs, every organization's oldest first, then each one's next, and so on.

    important: a claim refused for want of a slot changes nothing, so ordering by age alone
    would let one organization with more queued jobs than `limit` hold the whole list and
    starve the rest.
    """
    now = utcnow()
    ranked = (
        select(
            MigrationJob.job_id,
            MigrationJob.updated_at,
            func.row_number()
            .over(
                partition_by=MigrationJob.source_organization_id,
                order_by=MigrationJob.updated_at,
            )
            .label("turn"),
        )
        .where(
            MigrationJob.status.in_(CLAIMABLE_STATUSES),
            or_(
                MigrationJob.lease_expires_at.is_(None),
                MigrationJob.lease_expires_at < now,
            ),
        )
        .subquery()
    )
    return list(
        (
            await db.execute(
                select(ranked.c.job_id)
                .order_by(ranked.c.turn, ranked.c.updated_at)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


# An operator keeps at most this many open plans, and an expired plan no job refers to is
# removed after PLAN_RETENTION. A job's plan stays for its history.
OPEN_PLAN_LIMIT = 50
PLAN_RETENTION = timedelta(days=30)
_OPEN_PLAN_STATUSES = ("draft", "preflighting", "ready", "blocked")


async def discard_plan(db: AsyncSession, plan_id: str) -> bool:
    """Delete a plan no migration ran from. False when it was started (it stays with its job)."""
    started = select(MigrationJob.job_id).where(MigrationJob.plan_id == MigrationPlan.plan_id)
    result = await db.execute(
        delete(MigrationPlan).where(
            MigrationPlan.plan_id == plan_id,
            MigrationPlan.status != "executed",
            ~started.exists(),
        )
    )
    await db.commit()
    return bool(result.rowcount)


async def count_open_plans(db: AsyncSession, requester: str) -> int:
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(MigrationPlan)
                .where(
                    MigrationPlan.requester == requester,
                    MigrationPlan.status.in_(_OPEN_PLAN_STATUSES),
                    MigrationPlan.expires_at > utcnow(),
                )
            )
        ).scalar()
        or 0
    )


async def purge_stale_plans(db: AsyncSession) -> int:
    referenced = select(MigrationJob.job_id).where(MigrationJob.plan_id == MigrationPlan.plan_id)
    result = await db.execute(
        delete(MigrationPlan).where(
            MigrationPlan.status == "expired",
            MigrationPlan.updated_at < utcnow() - PLAN_RETENTION,
            ~referenced.exists(),
        )
    )
    await db.commit()
    return int(result.rowcount or 0)


async def mark_expired_plans(db: AsyncSession) -> int:
    result = await db.execute(
        update(MigrationPlan)
        .where(
            MigrationPlan.status.in_(("draft", "preflighting", "ready", "blocked")),
            MigrationPlan.expires_at <= utcnow(),
        )
        .values(status="expired", updated_at=utcnow())
    )
    await db.commit()
    return int(result.rowcount or 0)


async def pending_outbox_rows(db: AsyncSession, *, worker_id: str, limit: int = 50) -> list[MigrationAuditOutbox]:
    now = utcnow()
    rows = list(
        (
            await db.execute(
                select(MigrationAuditOutbox)
                .where(
                    MigrationAuditOutbox.status.in_(("pending", "failed", "delivering")),
                    or_(
                        MigrationAuditOutbox.next_attempt_at.is_(None),
                        MigrationAuditOutbox.next_attempt_at <= now,
                    ),
                    or_(
                        MigrationAuditOutbox.lease_expires_at.is_(None),
                        MigrationAuditOutbox.lease_expires_at < now,
                    ),
                )
                .order_by(MigrationAuditOutbox.created_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.status = "delivering"
        row.locked_by = worker_id
        row.lease_expires_at = now + LEASE_DURATION
        row.attempts += 1
    await db.commit()
    return rows


def outbox_retry_delay(attempts: int) -> timedelta:
    """30 s after the first failure, doubling, at most an hour: a receiver that is down for long
    is not called every 30 s forever."""
    # The exponent is capped first: 30 s doubled seven times already passes the hour.
    return min(timedelta(seconds=30 * 2 ** min(max(0, attempts - 1), 7)), timedelta(hours=1))


async def release_outbox(db: AsyncSession, event_ids: list[str], *, worker_id: str) -> None:
    """Hand claimed rows back untried, so the next drain takes them without waiting a lease out."""
    if not event_ids:
        return
    await db.execute(
        update(MigrationAuditOutbox)
        .where(
            MigrationAuditOutbox.event_id.in_(event_ids),
            MigrationAuditOutbox.locked_by == worker_id,
            MigrationAuditOutbox.status == "delivering",
        )
        .values(
            status="pending",
            locked_by=None,
            lease_expires_at=None,
            attempts=MigrationAuditOutbox.attempts - 1,
        )
    )
    await db.commit()


async def finish_outbox(
    db: AsyncSession,
    event_id: str,
    *,
    worker_id: str,
    delivered: bool,
    error: Optional[str] = None,
    attempts: int = 1,
) -> None:
    now = utcnow()
    values: dict[str, Any] = {
        "locked_by": None,
        "lease_expires_at": None,
        "last_error": sanitize_error(error) if error else None,
    }
    if delivered:
        values.update(status="delivered", delivered_at=now, next_attempt_at=None)
    else:
        values.update(status="failed", next_attempt_at=now + outbox_retry_delay(attempts))
    await db.execute(
        update(MigrationAuditOutbox)
        .where(
            MigrationAuditOutbox.event_id == event_id,
            MigrationAuditOutbox.locked_by == worker_id,
            MigrationAuditOutbox.status == "delivering",
        )
        .values(**values)
    )
    await db.commit()
