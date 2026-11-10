"""Authoritative guards for resources owned by an active migration.

Normal lifecycle and operation endpoints must not bypass Migration Center after a source or
staged target has been locked. The rollout flag controls new migration submissions, never the
durable fences of jobs that already exist.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session_maker
from app.models.migration_models import (
    MigrationJob,
    MigrationResource,
    MigrationResourceLock,
)
from app.models.db_models import OrganizationCluster

logger = logging.getLogger(__name__)

MIGRATION_CENTER_PATH = "/inventory/migrations"
MIGRATION_FENCE_DETAIL = (
    "This instance is fenced by an active migration. Open /inventory/migrations to continue, "
    "roll back, or clean up the retained source."
)
MIGRATION_FENCE_UNAVAILABLE_DETAIL = (
    "This instance action is temporarily blocked because migration fencing could not be "
    "verified. Open /inventory/migrations or retry shortly."
)
MIGRATION_PROJECT_FENCE_DETAIL = (
    "This project is fenced by an active migration. Open /inventory/migrations to continue, "
    "roll back, or clean up the migration."
)
MIGRATION_PROJECT_FENCE_UNAVAILABLE_DETAIL = (
    "This project action is temporarily blocked because migration fencing could not be "
    "verified. Open /inventory/migrations or retry shortly."
)
MIGRATION_CLUSTER_FENCE_DETAIL = (
    "This cluster has an active migration. Open /inventory/migrations before changing "
    "cluster-wide infrastructure."
)
MIGRATION_CLUSTER_FENCE_UNAVAILABLE_DETAIL = (
    "This cluster action is temporarily blocked because migration fencing could not be "
    "verified. Open /inventory/migrations or retry shortly."
)
MIGRATION_OPERATION_DETAIL = (
    "This operation belongs to an active migration and can only be managed from "
    "/inventory/migrations."
)


class MigrationFenceConflict(RuntimeError):
    """A normal resource write would cross a durable migration fence."""

    def __init__(self, detail: str = MIGRATION_FENCE_DETAIL) -> None:
        self.detail = detail
        super().__init__(detail)


@asynccontextmanager
async def guard_migration_mutations(
    db: Optional[AsyncSession] = None,
    *,
    clusters: Iterable[str] = (),
    projects: Iterable[tuple[str, str]] = (),
    instances: Iterable[tuple[str, str, str]] = (),
) -> AsyncIterator[None]:
    """Serialize ordinary writes against final migration admission.

    A caller may guard several related resources, such as every cluster referenced by one
    tenant policy. Locks are acquired once in canonical cluster order so two multi-cluster
    requests cannot deadlock each other. Fence reads happen only after every lock is held.

    When ``db`` is omitted, a private transaction owns the advisory locks and stays open across
    the commits of service methods that commit their request session internally. important: when
    ``db`` is supplied, a commit or rollback releases the locks and must be the last
    migration-relevant write in the context.
    """
    cluster_targets = {str(cluster_id) for cluster_id in clusters if str(cluster_id)}
    project_targets = {
        (str(cluster_id), str(project))
        for cluster_id, project in projects
        if str(cluster_id) and str(project)
    }
    instance_targets = {
        (str(cluster_id), str(project), str(instance_name))
        for cluster_id, project, instance_name in instances
        if str(cluster_id) and str(project) and str(instance_name)
    }
    lock_clusters = sorted(
        cluster_targets
        | {cluster_id for cluster_id, _project in project_targets}
        | {cluster_id for cluster_id, _project, _name in instance_targets}
    )
    if not lock_clusters:
        raise MigrationFenceConflict(MIGRATION_CLUSTER_FENCE_UNAVAILABLE_DETAIL)

    # Local import avoids the service-package bootstrap cycle:
    # shared_cluster_audit -> instance_security_policy -> rbac -> activity_logger -> services.
    from app.services.shared_cluster_audit import serialize_cluster_configuration

    @asynccontextmanager
    async def checked(session: AsyncSession) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            try:
                for cluster_id in lock_clusters:
                    await stack.enter_async_context(
                        serialize_cluster_configuration(session, cluster_id)
                    )

                for cluster_id in sorted(cluster_targets):
                    await assert_cluster_not_migration_fenced(
                        cluster_id=cluster_id,
                        db=session,
                    )
                for cluster_id, project in sorted(project_targets):
                    await assert_project_not_migration_fenced(
                        cluster_id=cluster_id,
                        project=project,
                        db=session,
                    )
                for cluster_id, project, instance_name in sorted(instance_targets):
                    await assert_instance_not_migration_fenced(
                        cluster_id=cluster_id,
                        project=project,
                        instance_name=instance_name,
                        db=session,
                    )
            except MigrationFenceConflict:
                raise
            except Exception as exc:  # noqa: BLE001 - admission must fail closed
                logger.error(
                    "Could not acquire migration mutation admission for %s cluster(s): %s",
                    len(lock_clusters),
                    type(exc).__name__,
                )
                raise MigrationFenceConflict(
                    MIGRATION_CLUSTER_FENCE_UNAVAILABLE_DETAIL
                ) from exc
            yield

    if db is not None:
        async with checked(db):
            yield
        return

    async with async_session_maker() as session:
        async with checked(session):
            yield


@asynccontextmanager
async def guard_project_mutation(
    db: Optional[AsyncSession] = None,
    *,
    cluster_id: str,
    project: str,
) -> AsyncIterator[None]:
    """Serialize a project-scoped metadata write with migration admission."""
    async with guard_migration_mutations(
        db,
        projects=((cluster_id, project),),
    ):
        yield


@asynccontextmanager
async def guard_cluster_mutation(
    db: Optional[AsyncSession] = None,
    *,
    cluster_id: str,
) -> AsyncIterator[None]:
    """Serialize a cluster-wide metadata write with migration admission."""
    async with guard_migration_mutations(db, clusters=(cluster_id,)):
        yield


@asynccontextmanager
async def guard_organization_mutation(
    db: Optional[AsyncSession] = None,
    *,
    organization_id: int,
) -> AsyncIterator[None]:
    """Serialize an organization ownership or authority write across its clusters.

    The binding set is read twice. A concurrent bind that won one of the locks must not let this
    request proceed using a stale, smaller cluster set.
    """

    @asynccontextmanager
    async def checked(session: AsyncSession) -> AsyncIterator[None]:
        initial = await _organization_cluster_ids(
            session,
            organization_id=organization_id,
        )
        if not initial:
            # An organization with no cluster cannot be referenced by an executable migration.
            yield
            return
        async with guard_migration_mutations(session, clusters=initial):
            current = await _organization_cluster_ids(
                session,
                organization_id=organization_id,
            )
            if current != initial:
                raise MigrationFenceConflict(MIGRATION_CLUSTER_FENCE_UNAVAILABLE_DETAIL)
            yield

    if db is not None:
        async with checked(db):
            yield
        return

    async with async_session_maker() as session:
        async with checked(session):
            yield


@asynccontextmanager
async def guard_instance_mutation(
    db: Optional[AsyncSession] = None,
    *,
    cluster_id: str,
    project: str,
    instance_name: str,
) -> AsyncIterator[None]:
    """Serialize one ordinary instance write with migration fence admission.

    The fence read and the daemon mutation must be one advisory-lock critical section. A
    standalone fence check has a check-then-act gap: migration execution can commit its durable
    resource locks after the read but before the caller submits a daemon write. Migration plan
    execution uses this same cluster serialization primitive, so exactly one side wins. If the
    migration wins, this guard observes its committed fence and yields no mutation authority. If
    the ordinary write wins, migration execution waits and runs fresh preflight after the write.

    important: keep this context open through the final daemon write and readback, and never
    commit or roll back the supplied session inside it: PostgreSQL releases transaction advisory
    locks at that boundary.
    """
    async with guard_migration_mutations(
        db,
        instances=((cluster_id, project, instance_name),),
    ):
        yield


async def _active_locked_job_id(
    db: AsyncSession,
    *,
    cluster_id: str,
    project: str,
    instance_name: str,
) -> Optional[str]:
    exact_instance = and_(
        MigrationResourceLock.lock_scope == "instance",
        MigrationResourceLock.cluster_id == cluster_id,
        MigrationResourceLock.project == project,
        MigrationResourceLock.resource_name == instance_name,
    )
    project_relocation = and_(
        MigrationJob.scope == "project",
        MigrationResourceLock.lock_scope == "project",
        MigrationResourceLock.cluster_id == cluster_id,
        MigrationResourceLock.project == project,
    )
    statement = (
        select(MigrationResourceLock.job_id)
        .join(MigrationJob, MigrationResourceLock.job_id == MigrationJob.job_id)
        .where(
            or_(
                MigrationJob.active_source_key.is_not(None),
                MigrationJob.active_target_key.is_not(None),
            ),
            or_(exact_instance, project_relocation),
        )
        .limit(1)
    )
    return (await db.execute(statement)).scalar_one_or_none()


# Rows that fence a whole project or cluster: every project lock, plus the instance locks of a
# retained move while its rollback or cleanup is mutating daemons.
_hierarchy_fence = or_(
    MigrationResourceLock.lock_scope == "project",
    MigrationJob.phase.in_(("rolling_back", "cleaning_up")),
)


async def _active_project_lock_job_id(
    db: AsyncSession,
    *,
    cluster_id: str,
    project: str,
) -> Optional[str]:
    """Return the job owning a project hierarchy lock, regardless of migration scope."""
    statement = (
        select(MigrationResourceLock.job_id)
        .join(MigrationJob, MigrationResourceLock.job_id == MigrationJob.job_id)
        .where(
            or_(
                MigrationJob.active_source_key.is_not(None),
                MigrationJob.active_target_key.is_not(None),
            ),
            _hierarchy_fence,
            MigrationResourceLock.cluster_id == cluster_id,
            MigrationResourceLock.project == project,
        )
        .limit(1)
    )
    return (await db.execute(statement)).scalar_one_or_none()


async def _active_cluster_lock_job_id(
    db: AsyncSession,
    *,
    cluster_id: str,
) -> Optional[str]:
    """Return any migration holding a project hierarchy in this cluster.

    A staged move in retention keeps only the exact lock on its stopped source. That instance
    fence must not freeze cluster-wide infrastructure for the whole retention window, so only
    project-scope locks, which every running migration holds, fence the cluster. A rollback or
    cleanup mutates daemons again, so its instance locks fence the cluster too.
    """
    statement = (
        select(MigrationResourceLock.job_id)
        .join(MigrationJob, MigrationResourceLock.job_id == MigrationJob.job_id)
        .where(
            or_(
                MigrationJob.active_source_key.is_not(None),
                MigrationJob.active_target_key.is_not(None),
            ),
            MigrationResourceLock.cluster_id == cluster_id,
            _hierarchy_fence,
        )
        .limit(1)
    )
    return (await db.execute(statement)).scalar_one_or_none()


async def _organization_cluster_ids(
    db: AsyncSession,
    *,
    organization_id: int,
) -> set[str]:
    rows = await db.execute(
        select(OrganizationCluster.cluster_id).where(
            OrganizationCluster.organization_id == organization_id
        )
    )
    return {str(cluster_id) for cluster_id in rows.scalars() if cluster_id}


async def assert_instance_not_migration_fenced(
    *,
    cluster_id: str,
    project: str,
    instance_name: str,
    db: Optional[AsyncSession] = None,
) -> None:
    """Block external mutation while Migration Center owns the exact resource.

    A database failure is a refusal while migration tables are installed. Allowing the write would be
    indistinguishable from proving that no active fence exists and could start an old source
    after the target has accepted writes.
    """
    try:
        if db is not None:
            job_id = await _active_locked_job_id(
                db,
                cluster_id=cluster_id,
                project=project,
                instance_name=instance_name,
            )
        else:
            async with async_session_maker() as session:
                job_id = await _active_locked_job_id(
                    session,
                    cluster_id=cluster_id,
                    project=project,
                    instance_name=instance_name,
                )
    except MigrationFenceConflict:
        raise
    except Exception as exc:  # noqa: BLE001 - enabled fencing must fail closed
        logger.error(
            "Could not verify the migration fence for %s/%s/%s: %s",
            cluster_id,
            project,
            instance_name,
            type(exc).__name__,
        )
        raise MigrationFenceConflict(MIGRATION_FENCE_UNAVAILABLE_DETAIL) from exc

    if job_id is not None:
        raise MigrationFenceConflict()


async def assert_project_not_migration_fenced(
    *,
    cluster_id: str,
    project: str,
    db: Optional[AsyncSession] = None,
) -> None:
    """Block writes anywhere under a project hierarchy while a migration owns it.

    Instance migrations take project locks as well as exact instance locks. This guard protects
    dependencies such as profiles, networks, ACLs, and custom volumes from drifting after
    preflight. The rollout flag does not apply to a lock that is already durable.
    """
    try:
        if db is not None:
            job_id = await _active_project_lock_job_id(
                db,
                cluster_id=cluster_id,
                project=project,
            )
        else:
            async with async_session_maker() as session:
                job_id = await _active_project_lock_job_id(
                    session,
                    cluster_id=cluster_id,
                    project=project,
                )
    except MigrationFenceConflict:
        raise
    except Exception as exc:  # noqa: BLE001 - durable fencing must fail closed
        logger.error(
            "Could not verify the migration project fence for %s/%s: %s",
            cluster_id,
            project,
            type(exc).__name__,
        )
        raise MigrationFenceConflict(MIGRATION_PROJECT_FENCE_UNAVAILABLE_DETAIL) from exc

    if job_id is not None:
        raise MigrationFenceConflict(MIGRATION_PROJECT_FENCE_DETAIL)


async def assert_cluster_not_migration_fenced(
    *,
    cluster_id: str,
    db: Optional[AsyncSession] = None,
) -> None:
    """Block cluster-wide infrastructure changes while any migration uses the cluster."""
    try:
        if db is not None:
            job_id = await _active_cluster_lock_job_id(db, cluster_id=cluster_id)
        else:
            async with async_session_maker() as session:
                job_id = await _active_cluster_lock_job_id(session, cluster_id=cluster_id)
    except MigrationFenceConflict:
        raise
    except Exception as exc:  # noqa: BLE001 - durable fencing must fail closed
        logger.error(
            "Could not verify the migration cluster fence for %s: %s",
            cluster_id,
            type(exc).__name__,
        )
        raise MigrationFenceConflict(MIGRATION_CLUSTER_FENCE_UNAVAILABLE_DETAIL) from exc

    if job_id is not None:
        raise MigrationFenceConflict(MIGRATION_CLUSTER_FENCE_DETAIL)


async def assert_organization_not_migration_fenced(
    *,
    organization_id: int,
    db: Optional[AsyncSession] = None,
) -> None:
    """Block ownership or membership writes for an organization on a migrating cluster."""

    async def _check(session: AsyncSession) -> None:
        cluster_ids = await _organization_cluster_ids(
            session,
            organization_id=organization_id,
        )
        for cluster_id in cluster_ids:
            if await _active_cluster_lock_job_id(session, cluster_id=cluster_id) is not None:
                raise MigrationFenceConflict(MIGRATION_CLUSTER_FENCE_DETAIL)

    try:
        if db is not None:
            await _check(db)
        else:
            async with async_session_maker() as session:
                await _check(session)
    except MigrationFenceConflict:
        raise
    except Exception as exc:  # noqa: BLE001 - durable ownership fencing must fail closed
        logger.error(
            "Could not verify the migration organization fence for %s: %s",
            organization_id,
            type(exc).__name__,
        )
        raise MigrationFenceConflict(MIGRATION_CLUSTER_FENCE_UNAVAILABLE_DETAIL) from exc


def _operation_ref_matches(value: Any, *, cluster_id: str, operation_id: str) -> bool:
    if not isinstance(value, list):
        return False
    return any(
        isinstance(item, Mapping)
        and item.get("cluster_id") == cluster_id
        and item.get("operation_id") == operation_id
        for item in value
    )


async def _migration_owns_operation(
    db: AsyncSession, *, cluster_id: str, operation_id: str
) -> bool:
    cluster_match = or_(
        MigrationJob.source_cluster_id == cluster_id,
        MigrationJob.target_cluster_id == cluster_id,
    )
    active_match = or_(
        MigrationJob.active_source_key.is_not(None),
        MigrationJob.active_target_key.is_not(None),
    )
    jobs = (
        await db.execute(
            select(MigrationJob.operation_refs).where(cluster_match, active_match)
        )
    ).scalars()
    if any(
        _operation_ref_matches(refs, cluster_id=cluster_id, operation_id=operation_id)
        for refs in jobs
    ):
        return True

    resources = (
        await db.execute(
            select(MigrationResource.operation_refs)
            .join(MigrationJob, MigrationResource.job_id == MigrationJob.job_id)
            .where(cluster_match, active_match)
        )
    ).scalars()
    return any(
        _operation_ref_matches(refs, cluster_id=cluster_id, operation_id=operation_id)
        for refs in resources
    )


async def assert_operation_not_migration_owned(
    *,
    cluster_id: str,
    operation_id: str,
    db: Optional[AsyncSession] = None,
) -> None:
    """Keep migration operation cancellation behind the durable job API."""
    try:
        if db is not None:
            owned = await _migration_owns_operation(
                db, cluster_id=cluster_id, operation_id=operation_id
            )
        else:
            async with async_session_maker() as session:
                owned = await _migration_owns_operation(
                    session, cluster_id=cluster_id, operation_id=operation_id
                )
    except MigrationFenceConflict:
        raise
    except Exception as exc:  # noqa: BLE001 - enabled fencing must fail closed
        logger.error(
            "Could not verify migration ownership for operation %s on %s: %s",
            operation_id,
            cluster_id,
            type(exc).__name__,
        )
        raise MigrationFenceConflict(MIGRATION_FENCE_UNAVAILABLE_DETAIL) from exc

    if owned:
        raise MigrationFenceConflict(MIGRATION_OPERATION_DETAIL)
