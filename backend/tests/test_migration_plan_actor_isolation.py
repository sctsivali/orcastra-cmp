"""Durable migration plans stay private to their exact Authentik actor."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Response

from app.core.rbac import Role
from app.models.migration_schemas import (
    MigrationExecuteRequest,
    MigrationPlanCreateRequest,
    MigrationPlanPatchRequest,
)
from app.routers import migrations as migration_router
from app.services.migrations import context as migration_context
from app.services.migrations import planning, repository
from app.services.migrations.authority import (
    ActorIdentityPin,
    CurrentMigrationAuthority,
    MigrationAuthorityPin,
)
from app.services.migrations.contracts import MigrationNotFound


OWNER_EMAIL = "plan-owner@example.test"
OWNER_SUBJECT = "1e2bb4b8-6e9f-4360-a718-4aa7648a06c6"
OWNER_PROVIDER_UUID = "ad4daef7-f39b-4d55-a20b-051a14016e2b"
OTHER_EMAIL = "other-partner@example.test"
OTHER_SUBJECT = "e6163b31-cd06-4103-bc0a-19c97e7854fb"
OTHER_PROVIDER_UUID = "257390c3-c337-41dc-9db5-21b37f9776e7"
ISSUER = "https://identity.example.test/application/o/orcastra/"


def _authority(
    *,
    email: str,
    subject: str,
    provider_uuid: str,
) -> CurrentMigrationAuthority:
    return CurrentMigrationAuthority(
        pin=MigrationAuthorityPin(
            identity=ActorIdentityPin(
                issuer=ISSUER,
                subject=subject,
                email=email,
            ),
            provider_uuid=provider_uuid,
        ),
        role=Role.PARTNER,
        mapped_groups=("role_partner",),
    )


def _owner_authority() -> CurrentMigrationAuthority:
    return _authority(
        email=OWNER_EMAIL,
        subject=OWNER_SUBJECT,
        provider_uuid=OWNER_PROVIDER_UUID,
    )


def _other_authority(*, reuse_owner_email: bool = False) -> CurrentMigrationAuthority:
    return _authority(
        email=OWNER_EMAIL if reuse_owner_email else OTHER_EMAIL,
        subject=OTHER_SUBJECT,
        provider_uuid=OTHER_PROVIDER_UUID,
    )


def _plan_row(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "plan_id": "owner-plan",
        "revision": 1,
        "requester": OWNER_EMAIL,
        "actor_issuer": ISSUER,
        "actor_subject": OWNER_SUBJECT,
        "actor_email": OWNER_EMAIL,
        "actor_provider_uuid": OWNER_PROVIDER_UUID,
        "source_organization_id": 17,
        "target_organization_id": 17,
        "source_cluster_id": "shared-cluster",
        "source_project": "source-project",
        "target_cluster_id": "shared-cluster",
        "target_project": "target-project",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def _allow_endpoint_access(*_args: Any, **_kwargs: Any) -> None:
    """Model a second Partner who already passed both endpoint access checks."""


def _router_service(row: Any) -> SimpleNamespace:
    return SimpleNamespace(
        get_plan=AsyncMock(return_value=row),
        list_plans=AsyncMock(return_value=([row], 1)),
        patch_plan=AsyncMock(),
        execute_plan=AsyncMock(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get", "patch", "execute"])
async def test_same_org_partner_cannot_read_patch_or_execute_another_actor_plan(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    row = _plan_row()
    service = _router_service(row)

    async def current(_user: Any) -> CurrentMigrationAuthority:
        return _other_authority()

    monkeypatch.setattr(migration_router, "_current_authority", current)
    monkeypatch.setattr(
        migration_router,
        "_authorize_row_current",
        _allow_endpoint_access,
    )
    monkeypatch.setattr(migration_router, "MigrationService", lambda _db: service)

    with pytest.raises(HTTPException) as raised:
        if operation == "get":
            await migration_router.get_plan(
                row.plan_id,
                response=Response(),
                user=SimpleNamespace(),
                db=object(),  # type: ignore[arg-type]
            )
        elif operation == "patch":
            await migration_router.patch_plan(
                row.plan_id,
                MigrationPlanPatchRequest(retention_hours=48),
                response=Response(),
                if_match='"1"',
                user=SimpleNamespace(),
                db=object(),  # type: ignore[arg-type]
            )
        else:
            await migration_router.execute_plan(
                row.plan_id,
                MigrationExecuteRequest(reason="Approved capacity relocation"),
                if_match='"1"',
                idempotency_key="other-partner-attempt",
                user=SimpleNamespace(),
                db=object(),  # type: ignore[arg-type]
                _readonly=None,
            )

    assert raised.value.status_code == 404
    assert raised.value.detail == "Not found"
    service.get_plan.assert_awaited_once_with(row.plan_id)
    service.patch_plan.assert_not_awaited()
    service.execute_plan.assert_not_awaited()


@pytest.mark.asyncio
async def test_plan_list_fails_closed_if_repository_returns_a_replacement_actor_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _plan_row()
    service = _router_service(row)

    async def current(_user: Any) -> CurrentMigrationAuthority:
        return _other_authority(reuse_owner_email=True)

    monkeypatch.setattr(migration_router, "_current_authority", current)
    monkeypatch.setattr(migration_router, "MigrationService", lambda _db: service)

    with pytest.raises(HTTPException) as raised:
        await migration_router.list_plans(
            user=SimpleNamespace(),
            db=object(),  # type: ignore[arg-type]
            limit=25,
            offset=0,
        )

    assert raised.value.status_code == 503
    visibility = service.list_plans.await_args.kwargs["visibility"]
    assert visibility.actor_email == OWNER_EMAIL
    assert visibility.actor_subject == OTHER_SUBJECT
    assert visibility.actor_provider_uuid == OTHER_PROVIDER_UUID


@pytest.mark.asyncio
async def test_incomplete_plan_actor_pin_is_concealed_as_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        migration_router,
        "_authorize_row_current",
        _allow_endpoint_access,
    )

    with pytest.raises(HTTPException) as raised:
        await migration_router._authorize_plan_current(
            _plan_row(actor_subject=None),
            authority=_owner_authority(),
            user=SimpleNamespace(),
            db=object(),  # type: ignore[arg-type]
        )

    assert raised.value.status_code == 404
    assert raised.value.detail == "Not found"


def test_service_conceals_an_incomplete_locked_actor_pin() -> None:
    with pytest.raises(MigrationNotFound, match="Migration plan not found"):
        planning._require_plan_actor(
            _plan_row(actor_provider_uuid=None),
            _owner_authority(),
        )


def _verifier_returning(authority: CurrentMigrationAuthority) -> type[Any]:
    class Verifier:
        def __init__(self, _settings: Any) -> None:
            pass

        async def pin_user(self, _user: Any) -> CurrentMigrationAuthority:
            return authority

    return Verifier


@pytest.mark.asyncio
async def test_locked_patch_cannot_repin_another_actors_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = object()
    row = _plan_row()
    original_pin = (
        row.actor_issuer,
        row.actor_subject,
        row.actor_email,
        row.actor_provider_uuid,
    )
    get_plan = AsyncMock(return_value=row)
    replace_plan = AsyncMock()
    monkeypatch.setattr(repository, "get_plan", get_plan)
    monkeypatch.setattr(repository, "replace_plan", replace_plan)
    monkeypatch.setattr(
        planning,
        "AuthentikAuthorityVerifier",
        _verifier_returning(_other_authority()),
    )
    service = planning.MigrationPlanningMixin(
        database,  # type: ignore[arg-type]
        lxd_service=object(),
        settings=SimpleNamespace(),
    )

    with pytest.raises(MigrationNotFound, match="Migration plan not found"):
        await service.patch_plan(
            row.plan_id,
            MigrationPlanPatchRequest(retention_hours=48),
            expected_revision=1,
            user=SimpleNamespace(),
            requester_role="partner",
        )

    get_plan.assert_awaited_once_with(database, row.plan_id, lock=True)
    replace_plan.assert_not_awaited()
    assert (
        row.actor_issuer,
        row.actor_subject,
        row.actor_email,
        row.actor_provider_uuid,
    ) == original_pin


@pytest.mark.asyncio
async def test_locked_execute_rejects_another_actor_before_job_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = object()
    row = _plan_row()
    get_job = AsyncMock(return_value=None)
    get_plan = AsyncMock(return_value=row)
    create_job = AsyncMock()
    monkeypatch.setattr(repository, "get_job_by_request", get_job)
    monkeypatch.setattr(repository, "get_plan", get_plan)
    monkeypatch.setattr(repository, "create_job", create_job)
    monkeypatch.setattr(
        planning,
        "AuthentikAuthorityVerifier",
        _verifier_returning(_other_authority()),
    )
    service = planning.MigrationPlanningMixin(
        database,  # type: ignore[arg-type]
        lxd_service=object(),
        settings=SimpleNamespace(),
    )

    with pytest.raises(MigrationNotFound, match="Migration plan not found"):
        await service.execute_plan(
            row.plan_id,
            expected_revision=1,
            idempotency_key="other-partner-attempt",
            reason="Approved capacity relocation",
            acknowledgements=[],
            user=SimpleNamespace(),
            requester_role="partner",
        )

    get_job.assert_awaited_once()
    get_plan.assert_awaited_once_with(database, row.plan_id, lock=True)
    create_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_idempotent_replay_keeps_working_for_the_exact_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _owner_authority()
    plan = _plan_row()
    replay = _plan_row(payload_hash="same-digest")
    get_job = AsyncMock(return_value=replay)
    get_plan = AsyncMock()
    job_plan = AsyncMock(return_value=plan)
    authoritative_clients = AsyncMock(return_value=(object(), object()))
    monkeypatch.setattr(repository, "get_job_by_request", get_job)
    monkeypatch.setattr(repository, "get_plan", get_plan)
    monkeypatch.setattr(migration_context, "_job_plan", job_plan)
    monkeypatch.setattr(
        migration_context,
        "_authoritative_clients",
        authoritative_clients,
    )
    service = planning.MigrationPlanningMixin(
        object(),  # type: ignore[arg-type]
        lxd_service=object(),
        settings=SimpleNamespace(),
    )

    row, created = await service._execute_plan_with_digest(
        plan.plan_id,
        expected_revision=1,
        idempotency_key="stable-replay",
        digest="same-digest",
        reason="Approved capacity relocation",
        acknowledgements=[],
        user=SimpleNamespace(),
        authority=authority,
    )

    assert row is replay
    assert created is False
    job_plan.assert_awaited_once_with(replay)
    authoritative_clients.assert_awaited_once_with(
        replay,
        current_authority=authority,
    )
    get_plan.assert_not_awaited()


@pytest.mark.asyncio
async def test_compatibility_replay_rejects_a_replacement_actor_before_digest_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replacement = _other_authority(reuse_owner_email=True)
    plan = _plan_row()
    replay = _plan_row(payload_hash="an-unrelated-digest")
    get_job = AsyncMock(return_value=replay)
    job_plan = AsyncMock(return_value=plan)
    authoritative_clients = AsyncMock()
    monkeypatch.setattr(repository, "get_job_by_request", get_job)
    monkeypatch.setattr(migration_context, "_job_plan", job_plan)
    monkeypatch.setattr(
        migration_context,
        "_authoritative_clients",
        authoritative_clients,
    )
    monkeypatch.setattr(
        planning,
        "AuthentikAuthorityVerifier",
        _verifier_returning(replacement),
    )
    request = MigrationPlanCreateRequest.model_validate(
        {
            "scope": "instance",
            "intent": "copy",
            "source": {
                "cluster_id": "shared-cluster",
                "project": "source-project",
                "resource_name": "workload",
            },
            "target": {
                "cluster_id": "shared-cluster",
                "project": "target-project",
                "resource_name": "workload-copy",
            },
        }
    )
    service = planning.MigrationPlanningMixin(
        object(),  # type: ignore[arg-type]
        lxd_service=object(),
        settings=SimpleNamespace(),
    )

    with pytest.raises(MigrationNotFound, match="Migration plan not found"):
        await service.execute_compatibility_copy(
            request,
            idempotency_key="reused-email-replay",
            reason="Approved compatibility copy",
            acknowledgements=[],
            user=SimpleNamespace(),
            requester_role="partner",
        )

    job_plan.assert_not_awaited()
    authoritative_clients.assert_not_awaited()
