"""Persistence and wire-contract guards for the durable Migration Center."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.models.migration_models import (
    MigrationAuditOutbox,
    MigrationJob,
    MigrationPlan,
    MigrationResource,
    MigrationResourceLock,
)
from app.models.migration_schemas import (
    MigrationEndpointRef,
    MigrationExecuteRequest,
    MigrationIntent,
    MigrationPlanCreateRequest,
    MigrationPlanPatchRequest,
    MigrationScope,
)


def _constraint_names(model, kind) -> set[str]:
    return {
        constraint.name
        for constraint in model.__table__.constraints
        if isinstance(constraint, kind) and constraint.name
    }


def _instance_request(**overrides):
    values = {
        "scope": MigrationScope.INSTANCE,
        "intent": MigrationIntent.MOVE,
        "source": MigrationEndpointRef(
            cluster_id="source-cluster",
            project="default",
            resource_name="web-01",
        ),
        "target": MigrationEndpointRef(
            cluster_id="target-cluster",
            project="production",
            resource_name="web-01",
        ),
    }
    values.update(overrides)
    return MigrationPlanCreateRequest(**values)


def test_tables_keep_only_sanitized_control_plane_projections() -> None:
    assert {
        MigrationPlan.__tablename__,
        MigrationJob.__tablename__,
        MigrationResourceLock.__tablename__,
        MigrationResource.__tablename__,
        MigrationAuditOutbox.__tablename__,
    } == {
        "migration_plans",
        "migration_jobs",
        "migration_resource_locks",
        "migration_resources",
        "migration_audit_outbox",
    }

    forbidden = {
        "certificate",
        "migration_secret",
        "websocket_secret",
        "cloud_init",
        "raw_config",
        "private_key",
        "token",
    }
    for model in (
        MigrationPlan,
        MigrationJob,
        MigrationResourceLock,
        MigrationResource,
        MigrationAuditOutbox,
    ):
        assert forbidden.isdisjoint(model.__table__.columns.keys())


def test_job_has_idempotency_fencing_and_portable_active_key_backstops() -> None:
    names = _constraint_names(MigrationJob, UniqueConstraint)
    assert {
        "uq_migration_job_request_key",
        "uq_migration_job_active_source",
        "uq_migration_job_active_target",
    } <= names
    assert {
        "payload_hash",
        "lease_expires_at",
        "heartbeat_at",
        "fence_token",
        "attempt_count",
        "revision",
    } <= set(MigrationJob.__table__.columns.keys())


def test_resource_lock_closes_cross_direction_and_project_hierarchy_races() -> None:
    names = _constraint_names(MigrationResourceLock, UniqueConstraint)
    assert "uq_migration_resource_lock_key" in names
    assert "uq_migration_resource_lock_job_key" in names

    ddl = str(CreateTable(MigrationResourceLock.__table__).compile(dialect=postgresql.dialect()))
    assert "UNIQUE (lock_key)" in ddl
    assert "lock_scope IN ('project', 'instance')" in ddl
    assert "lock_scope = 'project' AND resource_name IS NULL" in ddl


def test_plan_and_job_enforce_same_organization_and_bounded_retention() -> None:
    plan_checks = _constraint_names(MigrationPlan, CheckConstraint)
    job_checks = _constraint_names(MigrationJob, CheckConstraint)
    assert "ck_migration_plan_same_organization" in plan_checks
    assert "ck_migration_job_same_organization" in job_checks
    assert "ck_migration_plan_retention_hours" in plan_checks
    assert "ck_migration_job_retention_hours" in job_checks


def test_resource_and_outbox_rows_are_deduplicated_per_job() -> None:
    assert "uq_migration_resource_job_key" in _constraint_names(
        MigrationResource, UniqueConstraint
    )
    outbox_names = _constraint_names(MigrationAuditOutbox, UniqueConstraint)
    assert "uq_migration_outbox_event_id" in outbox_names
    assert "uq_migration_outbox_job_channel_sequence" in outbox_names


def test_instance_plan_requires_a_source_instance() -> None:
    with pytest.raises(ValidationError, match="source.resource_name"):
        _instance_request(
            source=MigrationEndpointRef(cluster_id="source-cluster", project="default")
        )


def test_project_plan_refuses_instance_names() -> None:
    with pytest.raises(ValidationError, match="cannot set source.resource_name"):
        MigrationPlanCreateRequest(
            scope=MigrationScope.PROJECT,
            source=MigrationEndpointRef(
                cluster_id="source-cluster",
                project="source-project",
                resource_name="must-not-be-here",
            ),
            target=MigrationEndpointRef(cluster_id="target-cluster", project="target-project"),
        )


@pytest.mark.parametrize("hours", [0, 169])
def test_retention_is_bounded_on_the_wire(hours: int) -> None:
    with pytest.raises(ValidationError):
        _instance_request(retention_hours=hours)


def test_patch_rejects_an_empty_or_unknown_change() -> None:
    with pytest.raises(ValidationError, match="At least one"):
        MigrationPlanPatchRequest()
    with pytest.raises(ValidationError, match="Extra inputs"):
        MigrationPlanPatchRequest.model_validate({"migration_secret": "not-accepted"})


def test_mapping_names_and_acknowledgements_are_bounded() -> None:
    with pytest.raises(ValidationError):
        _instance_request(mappings={"networks": {"n" * 256: "target"}})
    with pytest.raises(ValidationError):
        MigrationExecuteRequest(
            reason="Approved change ticket 1234",
            acknowledgements=["a" * 201],
        )


def test_valid_plan_defaults_to_the_approved_recovery_policy() -> None:
    request = _instance_request()
    assert request.retention_hours == 24
    assert request.checkpoint_requested is False
    assert request.external_cutover_required is False
    assert request.mappings.storage_pools == {}


def test_schema_has_no_secret_bearing_public_fields() -> None:
    schema = MigrationPlanCreateRequest.model_json_schema()
    serialized = str(schema).lower()
    for forbidden in (
        "migration_secret",
        "websocket_secret",
        "private_key",
        "cloud_init",
        "raw_config",
    ):
        assert forbidden not in serialized


def test_datetime_columns_remain_timezone_aware() -> None:
    now = datetime.now(timezone.utc)
    assert now.tzinfo is not None
    for model, columns in (
        (MigrationPlan, ("expires_at", "created_at", "updated_at")),
        (
            MigrationJob,
            (
                "retention_deadline",
                "lease_expires_at",
                "heartbeat_at",
                "created_at",
                "updated_at",
            ),
        ),
    ):
        for column in columns:
            assert model.__table__.columns[column].type.timezone is True
