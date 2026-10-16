"""Database-backed plan blockers that the daemon preflight cannot see."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import OrganizationCluster, TenantAccessPolicy, TenantPolicyRule
from app.models.migration_models import MigrationJob, MigrationResourceLock
from app.models.migration_schemas import MigrationPlanCreateRequest
from app.services.instance_security_policy import (
    InstanceSecurityPolicyViolation,
    prepare_project_create_config,
    resolve_cluster_write_policy,
    shared_payload_findings,
    shared_project_findings,
)

from .contracts import _enum_value
from .security import canonical_resource_key


def _blocker(code: str, message: str, remediation: str) -> dict[str, str]:
    return {"code": code, "severity": "blocker", "message": message, "remediation": remediation}


async def orcahub_managed_project(db: AsyncSession, cluster_id: str, project: str) -> bool:
    """Whether OrcaHub provisioned tenant access to this project (its billing follows it)."""
    statement = (
        select(TenantPolicyRule.id)
        .join(TenantAccessPolicy, TenantAccessPolicy.id == TenantPolicyRule.policy_id)
        .where(
            TenantAccessPolicy.source == "orcahub",
            TenantPolicyRule.cluster_id == cluster_id,
            TenantPolicyRule.project_name.in_((project, "*")),
        )
        .limit(1)
    )
    return (await db.execute(statement)).first() is not None


async def orcahub_blockers(
    db: AsyncSession, request: MigrationPlanCreateRequest, *, strategy: str, settings: Any
) -> list[dict[str, str]]:
    """Refuse identity changes that would move OrcaHub-billed workloads it cannot follow."""
    if getattr(settings, "migration_orcahub_identity_change_enabled", False):
        return []
    if strategy == "member_relocation":
        return []
    intent = _enum_value(request.intent)
    sides = [("target", request.target.cluster_id, request.target.project)]
    if intent == "move":
        sides.insert(0, ("source", request.source.cluster_id, request.source.project))
    blockers: list[dict[str, str]] = []
    for side, cluster_id, project in sides:
        if await orcahub_managed_project(db, cluster_id, project):
            blockers.append(
                _blocker(
                    "orcahub_managed_project_identity_change",
                    f"The {side} project '{project}' is provisioned and billed by OrcaHub. "
                    "Moving workloads in or out of it would leave OrcaHub billing the wrong "
                    "instance.",
                    "Move it between members of the same cluster, or ask an administrator to "
                    "move it through OrcaHub.",
                )
            )
    return blockers


async def shared_policy_blockers(
    db: AsyncSession,
    *,
    user: Any,
    request: MigrationPlanCreateRequest,
    contracts: dict[str, Any],
    target_project_config: Optional[dict[str, Any]],
    creates_project: bool = False,
) -> list[dict[str, str]]:
    """Apply the shared-cluster write policy a Partner's direct create would face.

    important: when the job creates the target project, ``target_project_config`` is the exact
    config the worker will create it with (the source clone). It must already pass the
    Partner project-create policy and the isolation invariant, because the worker creates it
    verbatim and the approval pins that config.
    """
    if user is None:
        return []
    policy = await resolve_cluster_write_policy(user, request.target.cluster_id, db)
    if not policy.requires_safe_allowlist:
        return []
    blockers: list[dict[str, str]] = []
    for name, contract in contracts.items():
        findings = shared_payload_findings(
            contract.expected_config, contract.expected_devices, existing_resource=True
        )
        if findings:
            fields = ", ".join(sorted({finding.field for finding in findings}))
            blockers.append(
                _blocker(
                    "shared_cluster_policy_violation",
                    f"Instance '{name}' needs host-level settings ({fields}) that are not "
                    "allowed on a shared cluster.",
                    "Remove those settings, or ask an administrator to perform the migration.",
                )
            )
    if creates_project:
        try:
            prepare_project_create_config(policy, target_project_config or {})
            refused = bool(shared_project_findings(target_project_config or {}))
        except InstanceSecurityPolicyViolation:
            refused = True
        if refused:
            blockers.append(
                _blocker(
                    "shared_cluster_target_project_not_isolated",
                    f"The new target project '{request.target.project}' would copy the source "
                    "project's settings, which are not isolated for a shared cluster.",
                    "Create the target project first from Projects, which applies the "
                    "shared-cluster isolation, then plan the move into it. An administrator "
                    "can also perform this migration.",
                )
            )
    elif target_project_config is not None and shared_project_findings(target_project_config):
        blockers.append(
            _blocker(
                "shared_cluster_project_not_isolated",
                "The target project is not isolated for shared-cluster use.",
                "Ask an administrator to restrict the target project first.",
            )
        )
    return blockers


async def stale_target_access_blockers(
    db: AsyncSession,
    request: MigrationPlanCreateRequest,
    organization_id: Optional[int],
) -> list[dict[str, str]]:
    """Refuse a move onto an identity that already carries exact tenant access rules.

    important: a rollback points every org-owned exact rule at the target identity back to the
    source. A rule left over from an earlier instance with the target name would land on the
    restored source and grant tenants access they never had there.
    """
    if organization_id is None or _enum_value(request.intent) != "move":
        return []
    if _enum_value(request.scope) == "project":
        return await _stale_project_rules(db, request)
    target_name = request.target.resource_name or request.source.resource_name
    source = (request.source.cluster_id, request.source.project, request.source.resource_name)
    if source == (request.target.cluster_id, request.target.project, target_name):
        return []
    owned = select(TenantAccessPolicy.id).where(
        TenantAccessPolicy.organization_id == organization_id,
        TenantAccessPolicy.source != "orcahub",
    )
    count = await db.scalar(
        select(func.count())
        .select_from(TenantPolicyRule)
        .where(
            TenantPolicyRule.policy_id.in_(owned),
            TenantPolicyRule.cluster_id == request.target.cluster_id,
            TenantPolicyRule.project_name == request.target.project,
            TenantPolicyRule.instance_name == target_name,
        )
    )
    if not count:
        return []
    return [
        _blocker(
            "target_access_rules_exist",
            f"Tenant access rules already name '{target_name}' in the target project. They "
            "would apply to the moved instance, and a rollback would carry them to the source.",
            "Remove those exact rules in Tenant Access first, or choose another destination name.",
        )
    ]


async def _stale_project_rules(
    db: AsyncSession, request: MigrationPlanCreateRequest
) -> list[dict[str, str]]:
    """A project relocation creates the target project, so no rule may name it yet."""
    if (request.source.cluster_id, request.source.project) == (
        request.target.cluster_id,
        request.target.project,
    ):
        return []
    count = await db.scalar(
        select(func.count())
        .select_from(TenantPolicyRule)
        .where(
            TenantPolicyRule.cluster_id == request.target.cluster_id,
            TenantPolicyRule.project_name == request.target.project,
        )
    )
    if not count:
        return []
    return [
        _blocker(
            "target_access_rules_exist",
            f"Tenant access rules already name project '{request.target.project}' on the "
            "target cluster. They would grant access to the relocated project.",
            "Remove those rules in Tenant Access first, or choose another project name.",
        )
    ]


async def project_job_inside(
    db: AsyncSession, endpoints: list[tuple[str, str]], *, except_job: Optional[str] = None
) -> Optional[tuple[str, str]]:
    """(job_id, phase) of an active job that holds a lock in, or reads or writes, a project.

    important: by its columns as well as its locks. A retained instance move keeps a lock on its
    source only, yet its live target in the other project is still its rollback object (security
    review, pass 2).
    """
    active = or_(MigrationJob.active_source_key.is_not(None), MigrationJob.active_target_key.is_not(None))
    by_lock = or_(
        *(
            and_(MigrationResourceLock.cluster_id == cluster_id, MigrationResourceLock.project == project)
            for cluster_id, project in endpoints
        )
    )
    by_endpoint = or_(
        *(
            or_(
                and_(MigrationJob.source_cluster_id == cluster_id, MigrationJob.source_project == project),
                and_(MigrationJob.target_cluster_id == cluster_id, MigrationJob.target_project == project),
            )
            for cluster_id, project in endpoints
        )
    )
    others = [MigrationJob.job_id != except_job] if except_job else []
    for statement in (
        select(MigrationJob.job_id, MigrationJob.phase).where(active, by_endpoint, *others).limit(1),
        select(MigrationJob.job_id, MigrationJob.phase)
        .join(MigrationResourceLock, MigrationResourceLock.job_id == MigrationJob.job_id)
        .where(active, by_lock, *others)
        .limit(1),
    ):
        row = (await db.execute(statement)).first()
        if row is not None:
            return str(row[0]), str(row[1])
    return None


async def project_lock_blockers(
    db: AsyncSession, request: MigrationPlanCreateRequest
) -> list[dict[str, str]]:
    """Name another migration that still holds anything inside the source or target project.

    important: a retained source of an earlier move is a stopped instance that only its own job
    may touch. A project relocation would otherwise stop, copy, or delete it.
    """
    if _enum_value(request.scope) != "project":
        return []
    endpoints = sorted(
        {
            (request.source.cluster_id, request.source.project),
            (request.target.cluster_id, request.target.project),
        }
    )
    row = await project_job_inside(db, endpoints)
    if row is None:
        return []
    retained = str(row[1]) in {"source_retained", "cleanup_due", "cleanup_blocked"}
    return [
        _blocker(
            "project_in_active_migration",
            (
                "An instance in this project is still the retained source or target of an "
                "earlier move."
                if retained
                else "Another migration is still running inside this project."
            ),
            (
                "Finalize or roll back the earlier migration first."
                if retained
                else "Wait for the other migration to finish."
            ),
        )
    ]


async def single_owner_clusters(db: AsyncSession, *cluster_ids: str) -> bool:
    """Whether every cluster is bound to exactly one organization."""
    for cluster_id in set(cluster_ids):
        count = await db.scalar(
            select(func.count())
            .select_from(OrganizationCluster)
            .where(OrganizationCluster.cluster_id == cluster_id)
        )
        if count != 1:
            return False
    return True


async def active_endpoint_blockers(
    db: AsyncSession, request: MigrationPlanCreateRequest
) -> list[dict[str, str]]:
    """Name an endpoint still owned by another job instead of failing at execute time."""
    scope = _enum_value(request.scope)
    source_name = request.source.resource_name
    target_name = request.target.resource_name or source_name
    source_key = canonical_resource_key(
        scope, request.source.cluster_id, request.source.project, source_name
    )
    target_key = canonical_resource_key(
        scope, request.target.cluster_id, request.target.project, target_name
    )
    # important: a project relocation keeps project keys active through its retention, while
    # its new project holds no lock. Any plan touching either project would otherwise make an
    # object the relocation's rollback later deletes another job's (security review).
    keys = {
        source_key,
        target_key,
        canonical_resource_key("project", request.source.cluster_id, request.source.project, None),
        canonical_resource_key("project", request.target.cluster_id, request.target.project, None),
    }
    statement = (
        select(MigrationJob.job_id, MigrationJob.phase, MigrationJob.scope)
        .where(
            or_(
                MigrationJob.active_source_key.in_(keys),
                MigrationJob.active_target_key.in_(keys),
            )
        )
        .limit(1)
    )
    row = (await db.execute(statement)).first()
    if row is None:
        return []
    phase = str(row[1])
    retained = phase in {"source_retained", "cleanup_due", "cleanup_blocked"}
    project = str(row[2]) == "project"
    return [
        _blocker(
            "endpoint_in_active_migration",
            (
                "This project is part of a project relocation that is still in retention."
                if project and retained
                else "This project is part of a project relocation that is still running."
                if project
                else "This instance is still the retained source or target of an earlier move."
                if retained
                else "Another migration is still running for this source or target."
            ),
            (
                "Finalize or roll back the earlier migration first."
                if retained
                else "Wait for the other migration to finish."
            ),
        )
    ]


__all__ = [
    "active_endpoint_blockers",
    "orcahub_blockers",
    "orcahub_managed_project",
    "project_lock_blockers",
    "shared_policy_blockers",
    "single_owner_clusters",
    "stale_target_access_blockers",
]
