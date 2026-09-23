from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects import postgresql

from app.core.database import get_db
from app.core.org_scope import org_scope_dependency
from app.core.rbac import Role, get_current_user
from app.models.migration_schemas import MigrationJobPhase, MigrationResourceState
from app.routers import migrations as migration_router
from app.services.migrations.metrics import (
    _duration_statement,
    collect_migration_metrics,
)
from tests._project_grant_fakes import admin, partner


class _Result:
    def __init__(
        self,
        *,
        rows: list[tuple[Any, ...]] | None = None,
        one: tuple[Any, ...] | None = None,
        scalar: Any = None,
    ) -> None:
        self._rows = rows or []
        self._one = one
        self._scalar = scalar

    def all(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def one(self) -> tuple[Any, ...]:
        assert self._one is not None
        return self._one

    def scalar_one(self) -> Any:
        return self._scalar


class _MetricsSession:
    def __init__(self) -> None:
        self._results = iter(
            [
                _Result(rows=[("queued", 2), ("completed", 3), ("future-phase", 1)]),
                _Result(rows=[("pending", 2), ("success", 3), ("future-status", 1)]),
                _Result(rows=[("syncing", 4), ("completed", 5)]),
                _Result(rows=[("queued", 2, 4.5, 7.0), ("failed", 1, -1, -1)]),
                _Result(one=(7, 3)),
                _Result(one=(2, 8)),
                _Result(one=(4, 1)),
                _Result(scalar=5),
                _Result(scalar=1),
            ]
        )
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return next(self._results)


@pytest.mark.asyncio
async def test_metrics_are_bounded_aggregates_with_explicit_unavailable_sources() -> None:
    session = _MetricsSession()

    result = await collect_migration_metrics(session)  # type: ignore[arg-type]

    assert result.jobs_total == 6
    assert result.jobs_by_phase["queued"] == 2
    assert result.jobs_by_phase["unrecognized"] == 1
    assert set(result.jobs_by_phase) == {
        *(phase.value for phase in MigrationJobPhase),
        "unrecognized",
    }
    assert result.jobs_by_outcome == {
        "in_progress": 2,
        "awaiting_operator": 0,
        "success": 3,
        "failure": 0,
        "cancelled": 0,
        "unknown": 0,
        "manual_recovery": 0,
        "unrecognized": 1,
    }
    assert result.phase_duration_seconds["queued"].sample_count == 2
    assert result.phase_duration_seconds["queued"].average_seconds == 4.5
    assert result.phase_duration_seconds["failed"].average_seconds == 0
    assert result.transfer_progress.resources_total == 9
    assert result.transfer_progress.resources_by_state["syncing"] == 4
    assert set(result.transfer_progress.resources_by_state) == {
        *(state.value for state in MigrationResourceState),
        "unrecognized",
    }
    assert result.transfer_progress.byte_progress.available is False
    assert result.reconciliation.events_total == 7
    assert result.reconciliation.jobs_total == 3
    assert result.rollback.completion_rate == 0.25
    assert result.cleanup_overdue.jobs_total == 4
    assert result.authorization_denials.denied_requests_total == 5
    assert result.orphan_detection.orphaned_lock_rows == 1
    assert result.orphan_detection.daemon_resources.available is False
    assert len(session.statements) == 9


def test_phase_duration_query_uses_only_durable_audit_phase_transitions() -> None:
    statement = _duration_statement(datetime(2026, 9, 22, tzinfo=timezone.utc))
    sql = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()

    assert "migration_audit_outbox" in sql
    assert "lag(" in sql
    assert "lead(" in sql
    assert "group by" in sql
    assert "requester" not in sql
    assert "source_cluster_id" not in sql
    assert "source_project" not in sql


def _metrics_payload() -> dict[str, Any]:
    return {
        "generated_at": "2026-09-22T12:00:00Z",
        "jobs_total": 0,
        "jobs_by_phase": {},
        "jobs_by_outcome": {},
        "phase_duration_seconds": {},
        "transfer_progress": {
            "resources_total": 0,
            "resources_by_state": {},
            "byte_progress": {"available": False, "reason": "Not persisted."},
        },
        "reconciliation": {"events_total": 0, "jobs_total": 0},
        "rollback": {"completed_jobs": 0, "move_jobs": 0, "completion_rate": 0},
        "cleanup_overdue": {"jobs_total": 0, "blocked_jobs": 0},
        "authorization_denials": {
            "available": True,
            "denied_requests_total": 0,
            "coverage": "Durable RBAC denials only.",
        },
        "orphan_detection": {
            "control_plane_available": True,
            "orphaned_lock_rows": 0,
            "daemon_resources": {"available": False, "reason": "Not persisted."},
        },
    }


def _app(user: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, AsyncMock]:
    app = FastAPI()
    app.include_router(migration_router.router)
    app.dependency_overrides[migration_router.require_migration_center] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: user

    async def _db():
        yield object()

    app.dependency_overrides[get_db] = _db
    collector = AsyncMock(return_value=_metrics_payload())
    monkeypatch.setattr(migration_router, "collect_migration_metrics", collector)
    return app, collector


@pytest.mark.asyncio
async def test_metrics_route_is_admin_only_and_never_queries_for_a_partner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, collector = _app(partner(), monkeypatch)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/migrations/metrics")

    assert response.status_code == 403
    collector.assert_not_awaited()


@pytest.mark.asyncio
async def test_metrics_route_rechecks_current_admin_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, collector = _app(admin(), monkeypatch)
    monkeypatch.setattr(
        migration_router,
        "_current_authority",
        AsyncMock(return_value=SimpleNamespace(role=Role.ADMIN)),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/migrations/metrics")

    assert response.status_code == 200, response.text
    collector.assert_awaited_once()


def test_metrics_literal_route_precedes_the_job_slug() -> None:
    paths = [route.path for route in migration_router.router.routes]

    assert paths.index("/migrations/metrics") < paths.index("/migrations/{job_id}")
