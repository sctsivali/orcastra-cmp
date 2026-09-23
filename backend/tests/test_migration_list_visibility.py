"""Migration list pagination is computed only after current authorization filters."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import JSON, MetaData, create_engine, delete, insert, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.models.db_models import (
    Organization,
    OrganizationCluster,
    OrganizationMember,
    TenantAccessPolicy,
    TenantPolicyRule,
)
from app.models.migration_models import MigrationJob, MigrationPlan
from app.services.migrations import repository


ACTOR_EMAIL = "migration-operator@example.test"
ACTOR_ISSUER = "https://identity.example.test/application/o/orcastra/"
ACTOR_SUBJECT = "signed-subject-for-the-current-actor"
ACTOR_UUID = "e31031a8-b48c-4276-a216-7497db3973cc"
OTHER_SUBJECT = "signed-subject-for-a-replacement-actor"
OTHER_UUID = "3f216a1a-2166-4fb8-8581-aeaf55683743"
NOW = datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc)


class _AsyncSessionAdapter:
    def __init__(self, session: Session) -> None:
        self.session = session

    async def execute(self, statement: Any) -> Any:
        return self.session.execute(statement)


@pytest.fixture
def migration_database() -> tuple[_AsyncSessionAdapter, Session]:
    engine = create_engine("sqlite:///:memory:")
    source_tables = [
        Organization.__table__,
        OrganizationMember.__table__,
        OrganizationCluster.__table__,
        TenantAccessPolicy.__table__,
        TenantPolicyRule.__table__,
        MigrationPlan.__table__,
        MigrationJob.__table__,
    ]
    metadata = MetaData()
    tables = [table.to_metadata(metadata) for table in source_tables]
    for table in tables:
        table.indexes.clear()
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
            column.server_default = None
    metadata.create_all(engine)
    session = Session(engine)
    try:
        yield _AsyncSessionAdapter(session), session
    finally:
        session.close()
        engine.dispose()


def _visibility(
    *,
    role: str = "partner",
    subject: str = ACTOR_SUBJECT,
    provider_uuid: str = ACTOR_UUID,
    allowed_clusters: frozenset[str] | None = None,
    visible_projects: tuple[tuple[str, frozenset[str]], ...] | None = None,
) -> repository.MigrationListVisibility:
    return repository.MigrationListVisibility(
        role=role,
        actor_issuer=ACTOR_ISSUER,
        actor_subject=subject,
        actor_email=ACTOR_EMAIL,
        actor_provider_uuid=provider_uuid,
        allowed_clusters=allowed_clusters,
        visible_projects=visible_projects,
    )


def _seed_organization(
    session: Session,
    *,
    organization_id: int,
    owner: str,
    member_email: str | None,
    clusters: tuple[str, ...],
) -> None:
    session.execute(
        insert(Organization).values(
            id=organization_id,
            name=f"Organization {organization_id}",
            slug=f"organization-{organization_id}",
            created_by=owner,
            is_active=True,
            source="local",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    if member_email is not None:
        session.execute(
            insert(OrganizationMember).values(
                id=organization_id * 100,
                organization_id=organization_id,
                user_email=member_email,
                role="member",
                joined_at=NOW,
            )
        )
    for index, cluster_id in enumerate(clusters, start=1):
        session.execute(
            insert(OrganizationCluster).values(
                id=organization_id * 100 + index,
                organization_id=organization_id,
                cluster_id=cluster_id,
                registered_by=owner,
                registered_at=NOW,
            )
        )


def _seed_project_grant(
    session: Session,
    *,
    organization_id: int,
    cluster_id: str,
    project: str,
) -> None:
    policy_id = organization_id * 1000
    session.execute(
        insert(TenantAccessPolicy).values(
            id=policy_id,
            organization_id=organization_id,
            name=f"Project ownership {organization_id}",
            is_default=False,
            source="local",
            created_by="system@example.test",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.execute(
        insert(TenantPolicyRule).values(
            id=policy_id,
            policy_id=policy_id,
            cluster_id=cluster_id,
            project_name=project,
            instance_name="*",
            permissions=["view"],
            created_at=NOW,
        )
    )


def _plan_values(
    row_id: int,
    *,
    subject: str = ACTOR_SUBJECT,
    provider_uuid: str = ACTOR_UUID,
) -> dict[str, Any]:
    return {
        "id": row_id,
        "plan_id": f"plan-{row_id}",
        "revision": 1,
        "scope": "instance",
        "intent": "move",
        "status": "ready",
        "requester": ACTOR_EMAIL,
        "requester_role": "partner",
        "actor_issuer": ACTOR_ISSUER,
        "actor_subject": subject,
        "actor_email": ACTOR_EMAIL,
        "actor_provider_uuid": provider_uuid,
        "source_organization_id": 1,
        "target_organization_id": 1,
        "source_cluster_id": "source-cluster",
        "source_project": "default",
        "source_resource_name": f"instance-{row_id}",
        "source_cluster_fingerprint": "source-fingerprint",
        "target_cluster_id": "target-cluster",
        "target_project": "default",
        "target_resource_name": f"instance-{row_id}",
        "target_cluster_fingerprint": "target-fingerprint",
        "source_key": f"source-key-{row_id}",
        "target_key": f"target-key-{row_id}",
        "payload_hash": f"{row_id:064x}",
        "request_payload": {},
        "preflight_payload": {},
        "retention_hours": 24,
        "checkpoint_requested": False,
        "external_cutover_required": False,
        "expires_at": NOW + timedelta(minutes=15),
        "created_at": NOW,
        "updated_at": NOW,
    }


def _job_values(
    row_id: int,
    *,
    source_cluster: str,
    source_project: str,
    target_cluster: str,
    target_project: str = "default",
) -> dict[str, Any]:
    return {
        "id": row_id,
        "job_id": f"job-{row_id}",
        "plan_id": f"accepted-plan-{row_id}",
        "plan_revision": 1,
        "revision": 1,
        "scope": "instance",
        "intent": "move",
        "requester": ACTOR_EMAIL,
        "requester_role": "partner",
        "actor_issuer": ACTOR_ISSUER,
        "actor_subject": ACTOR_SUBJECT,
        "actor_email": ACTOR_EMAIL,
        "actor_provider_uuid": ACTOR_UUID,
        "source_organization_id": 1,
        "target_organization_id": 1,
        "source_cluster_id": source_cluster,
        "source_project": source_project,
        "source_resource_name": f"instance-{row_id}",
        "source_cluster_fingerprint": "source-fingerprint",
        "target_cluster_id": target_cluster,
        "target_project": target_project,
        "target_resource_name": f"instance-{row_id}",
        "target_cluster_fingerprint": "target-fingerprint",
        "source_key": f"source-key-{row_id}",
        "target_key": f"target-key-{row_id}",
        "idempotency_key": f"request-{row_id}",
        "payload_hash": f"{row_id:064x}",
        "plan_payload_hash": f"{row_id:064x}",
        "strategy": "staged_cold",
        "downtime_class": "required",
        "phase": "queued",
        "status": "pending",
        "retention_hours": 24,
        "external_cutover_required": False,
        "operation_refs": [],
        "fence_token": 0,
        "attempt_count": 0,
        "created_at": NOW,
        "updated_at": NOW,
    }


@pytest.mark.asyncio
async def test_plan_count_and_offsets_exclude_same_email_replacement_rows(
    migration_database: tuple[_AsyncSessionAdapter, Session],
) -> None:
    db, session = migration_database
    _seed_organization(
        session,
        organization_id=1,
        owner="organization-owner@example.test",
        member_email=ACTOR_EMAIL,
        clusters=("source-cluster", "target-cluster"),
    )
    session.execute(
        insert(MigrationPlan),
        [
            _plan_values(1),
            _plan_values(2, subject=OTHER_SUBJECT, provider_uuid=OTHER_UUID),
            _plan_values(3),
            _plan_values(4, subject=OTHER_SUBJECT, provider_uuid=OTHER_UUID),
        ],
    )
    session.commit()

    visibility = _visibility(allowed_clusters=frozenset({"source-cluster", "target-cluster"}))
    first, total = await repository.list_plans(db, visibility, limit=1, offset=0)
    second, second_total = await repository.list_plans(db, visibility, limit=1, offset=1)
    beyond, beyond_total = await repository.list_plans(db, visibility, limit=1, offset=2)

    assert [row.id for row in first] == [3]
    assert [row.id for row in second] == [1]
    assert beyond == []
    assert total == second_total == beyond_total == 2


@pytest.mark.asyncio
async def test_partner_job_pagination_applies_both_endpoint_and_project_authority(
    migration_database: tuple[_AsyncSessionAdapter, Session],
) -> None:
    db, session = migration_database
    _seed_organization(
        session,
        organization_id=1,
        owner="organization-owner@example.test",
        member_email=ACTOR_EMAIL,
        clusters=("shared-cluster", "target-cluster"),
    )
    _seed_organization(
        session,
        organization_id=2,
        owner="other-owner@example.test",
        member_email=None,
        clusters=("shared-cluster", "revoked-cluster"),
    )
    _seed_project_grant(
        session,
        organization_id=1,
        cluster_id="shared-cluster",
        project="owned-project",
    )
    session.execute(
        insert(MigrationJob),
        [
            _job_values(
                1,
                source_cluster="shared-cluster",
                source_project="default",
                target_cluster="target-cluster",
            ),
            _job_values(
                2,
                source_cluster="revoked-cluster",
                source_project="default",
                target_cluster="target-cluster",
            ),
            _job_values(
                3,
                source_cluster="shared-cluster",
                source_project="other-project",
                target_cluster="target-cluster",
            ),
            _job_values(
                4,
                source_cluster="shared-cluster",
                source_project="owned-project",
                target_cluster="revoked-cluster",
            ),
            _job_values(
                5,
                source_cluster="shared-cluster",
                source_project="owned-project",
                target_cluster="target-cluster",
            ),
        ],
    )
    session.commit()

    visibility = _visibility(allowed_clusters=frozenset({"shared-cluster", "target-cluster", "revoked-cluster"}))
    first, total = await repository.list_jobs(
        db,
        visibility=visibility,
        limit=1,
        offset=0,
    )
    second, second_total = await repository.list_jobs(
        db,
        visibility=visibility,
        limit=1,
        offset=1,
    )
    beyond, beyond_total = await repository.list_jobs(
        db,
        visibility=visibility,
        limit=1,
        offset=2,
    )

    assert [row.id for row in first] == [5]
    assert [row.id for row in second] == [1]
    assert beyond == []
    assert total == second_total == beyond_total == 2

    session.execute(delete(OrganizationMember).where(OrganizationMember.user_email == ACTOR_EMAIL))
    session.commit()
    rows_after_revoke, total_after_revoke = await repository.list_jobs(
        db,
        visibility=visibility,
        limit=10,
        offset=0,
    )
    assert rows_after_revoke == []
    assert total_after_revoke == 0


@pytest.mark.asyncio
async def test_request_project_bounds_precede_admin_and_partner_pagination(
    migration_database: tuple[_AsyncSessionAdapter, Session],
) -> None:
    db, session = migration_database
    _seed_organization(
        session,
        organization_id=1,
        owner="organization-owner@example.test",
        member_email=ACTOR_EMAIL,
        clusters=("shared-cluster", "target-cluster"),
    )
    _seed_organization(
        session,
        organization_id=2,
        owner="other-owner@example.test",
        member_email=None,
        clusters=("shared-cluster",),
    )
    _seed_project_grant(
        session,
        organization_id=1,
        cluster_id="shared-cluster",
        project="owned-project",
    )
    session.execute(
        insert(MigrationJob),
        [
            _job_values(
                1,
                source_cluster="shared-cluster",
                source_project="default",
                target_cluster="target-cluster",
            ),
            _job_values(
                2,
                source_cluster="shared-cluster",
                source_project="owned-project",
                target_cluster="target-cluster",
            ),
        ],
    )
    session.commit()

    project_bound = (("shared-cluster", frozenset({"default"})),)
    partner_rows, partner_total = await repository.list_jobs(
        db,
        visibility=_visibility(
            allowed_clusters=frozenset({"shared-cluster", "target-cluster"}),
            visible_projects=project_bound,
        ),
        limit=10,
        offset=0,
    )
    admin_rows, admin_total = await repository.list_jobs(
        db,
        visibility=_visibility(
            role="admin",
            allowed_clusters=frozenset({"shared-cluster", "target-cluster"}),
            visible_projects=project_bound,
        ),
        limit=10,
        offset=0,
    )
    unrestricted_admin_rows, unrestricted_admin_total = await repository.list_jobs(
        db,
        visibility=_visibility(role="admin"),
        limit=10,
        offset=0,
    )

    assert [row.id for row in partner_rows] == [1]
    assert partner_total == 1
    assert [row.id for row in admin_rows] == [1]
    assert admin_total == 1
    assert [row.id for row in unrestricted_admin_rows] == [2, 1]
    assert unrestricted_admin_total == 2


@pytest.mark.asyncio
async def test_empty_request_scope_tenant_role_and_admin_plan_privacy_fail_closed(
    migration_database: tuple[_AsyncSessionAdapter, Session],
) -> None:
    db, session = migration_database
    _seed_organization(
        session,
        organization_id=1,
        owner="organization-owner@example.test",
        member_email=ACTOR_EMAIL,
        clusters=("source-cluster", "target-cluster"),
    )
    session.execute(
        insert(MigrationPlan), [_plan_values(1), _plan_values(2, subject=OTHER_SUBJECT, provider_uuid=OTHER_UUID)]
    )
    session.execute(
        insert(MigrationJob),
        [_job_values(1, source_cluster="source-cluster", source_project="default", target_cluster="target-cluster")],
    )
    session.commit()

    empty_scope_rows, empty_scope_total = await repository.list_jobs(
        db,
        visibility=_visibility(allowed_clusters=frozenset()),
        limit=10,
        offset=0,
    )
    tenant_rows, tenant_total = await repository.list_jobs(
        db,
        visibility=_visibility(role="tenant"),
        limit=10,
        offset=0,
    )
    admin_plans, admin_plan_total = await repository.list_plans(
        db,
        _visibility(role="admin"),
        limit=10,
        offset=0,
    )

    assert empty_scope_rows == []
    assert empty_scope_total == 0
    assert tenant_rows == []
    assert tenant_total == 0
    assert [row.id for row in admin_plans] == [1]
    assert admin_plan_total == 1


@pytest.mark.asyncio
async def test_partner_list_rechecks_active_org_and_both_endpoint_bindings(
    migration_database: tuple[_AsyncSessionAdapter, Session],
) -> None:
    db, session = migration_database
    _seed_organization(
        session,
        organization_id=1,
        owner="organization-owner@example.test",
        member_email=ACTOR_EMAIL,
        clusters=("source-cluster", "target-cluster"),
    )
    session.execute(
        insert(MigrationJob),
        [_job_values(1, source_cluster="source-cluster", source_project="default", target_cluster="target-cluster")],
    )
    session.commit()
    visibility = _visibility(allowed_clusters=frozenset({"source-cluster", "target-cluster"}))

    initial, initial_total = await repository.list_jobs(db, visibility=visibility, limit=10, offset=0)
    assert [row.id for row in initial] == [1]
    assert initial_total == 1

    session.execute(update(Organization).where(Organization.id == 1).values(is_active=False))
    session.commit()
    inactive, inactive_total = await repository.list_jobs(db, visibility=visibility, limit=10, offset=0)
    assert inactive == []
    assert inactive_total == 0

    session.execute(update(Organization).where(Organization.id == 1).values(is_active=True))
    session.execute(
        delete(OrganizationCluster).where(
            OrganizationCluster.organization_id == 1,
            OrganizationCluster.cluster_id == "target-cluster",
        )
    )
    session.commit()
    target_revoked, target_revoked_total = await repository.list_jobs(db, visibility=visibility, limit=10, offset=0)
    assert target_revoked == []
    assert target_revoked_total == 0


@pytest.mark.asyncio
async def test_shared_project_grant_and_unbound_owner_are_rechecked_before_count(
    migration_database: tuple[_AsyncSessionAdapter, Session],
) -> None:
    db, session = migration_database
    _seed_organization(
        session,
        organization_id=1,
        owner="organization-owner@example.test",
        member_email=ACTOR_EMAIL,
        clusters=("source-cluster", "target-cluster"),
    )
    session.execute(
        insert(MigrationJob),
        [
            _job_values(
                1, source_cluster="source-cluster", source_project="owned-project", target_cluster="target-cluster"
            )
        ],
    )
    session.commit()
    visibility = _visibility(allowed_clusters=frozenset({"source-cluster", "target-cluster"}))

    before_shared, before_shared_total = await repository.list_jobs(db, visibility=visibility, limit=10, offset=0)
    assert [row.id for row in before_shared] == [1]
    assert before_shared_total == 1

    session.execute(
        insert(OrganizationCluster).values(
            id=999,
            organization_id=None,
            cluster_id="source-cluster",
            registered_by="unbound-registrant@example.test",
            registered_at=NOW,
        )
    )
    session.commit()
    without_grant, without_grant_total = await repository.list_jobs(db, visibility=visibility, limit=10, offset=0)
    assert without_grant == []
    assert without_grant_total == 0

    _seed_project_grant(
        session,
        organization_id=1,
        cluster_id="source-cluster",
        project="owned-project",
    )
    session.commit()
    with_grant, with_grant_total = await repository.list_jobs(db, visibility=visibility, limit=10, offset=0)
    assert [row.id for row in with_grant] == [1]
    assert with_grant_total == 1

    session.execute(delete(TenantPolicyRule))
    session.commit()
    after_revoke, after_revoke_total = await repository.list_jobs(db, visibility=visibility, limit=10, offset=0)
    assert after_revoke == []
    assert after_revoke_total == 0


@pytest.mark.asyncio
async def test_corrupt_cross_organization_row_is_hidden_even_from_admin(
    migration_database: tuple[_AsyncSessionAdapter, Session],
) -> None:
    db, session = migration_database
    _seed_organization(
        session,
        organization_id=1,
        owner="organization-owner@example.test",
        member_email=ACTOR_EMAIL,
        clusters=("source-cluster", "target-cluster"),
    )
    _seed_organization(
        session,
        organization_id=2,
        owner="other-owner@example.test",
        member_email=None,
        clusters=(),
    )
    # Simulate storage corruption or an import that bypassed the production CHECK constraint.
    # The list authorization predicate must remain fail-closed independently of schema health.
    session.execute(text("PRAGMA ignore_check_constraints = ON"))
    corrupted = _job_values(
        1,
        source_cluster="source-cluster",
        source_project="default",
        target_cluster="target-cluster",
    )
    corrupted["target_organization_id"] = 2
    session.execute(insert(MigrationJob).values(**corrupted))
    session.commit()

    partner_rows, partner_total = await repository.list_jobs(
        db,
        visibility=_visibility(allowed_clusters=frozenset({"source-cluster", "target-cluster"})),
        limit=10,
        offset=0,
    )
    admin_rows, admin_total = await repository.list_jobs(
        db,
        visibility=_visibility(role="admin"),
        limit=10,
        offset=0,
    )

    assert partner_rows == []
    assert partner_total == 0
    assert admin_rows == []
    assert admin_total == 0
