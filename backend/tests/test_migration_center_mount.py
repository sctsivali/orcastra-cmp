"""Migration admission can stop without hiding or stranding durable recovery.

Building the FastAPI application outside a TestClient context does not run its lifespan, so these
checks are hermetic: no database, Vault, Authentik, Docker, LXD, or Incus call is made.
"""

from collections.abc import Iterator

import pytest

from app.core.config import get_settings
from app.core.public_openapi import _walk_route_paths
from app.main import create_app


@pytest.fixture
def migration_center_flag(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


def _paths() -> set[str]:
    # The served paths, however FastAPI nests an include (flat up to 0.128, nested from 0.141).
    return {path for path, _route in _walk_route_paths(create_app())}


def test_recovery_routes_remain_mounted_while_new_admission_is_off(
    migration_center_flag: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIGRATION_CENTER_ENABLED", "false")
    get_settings.cache_clear()

    paths = _paths()

    assert "/api/v1/migrations/plans" in paths
    assert "/api/v1/migrations/{job_id}" in paths
    assert "/api/v1/migrations/{job_id}/cleanup" in paths


def test_migration_routes_are_mounted_when_the_rollout_gate_is_on(
    migration_center_flag: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIGRATION_CENTER_ENABLED", "true")
    get_settings.cache_clear()

    paths = _paths()

    assert "/api/v1/migrations/plans" in paths
    assert "/api/v1/migrations/plans/{plan_id}/execute" in paths
    assert "/api/v1/migrations/{job_id}/cleanup" in paths


def test_lifespan_always_contains_both_durable_drains() -> None:
    import inspect

    from app import main

    source = inspect.getsource(main.lifespan)

    assert "_recover_migrations_once" in source
    assert "migration_recovery_task.cancel()" in source


def test_rollout_gate_blocks_only_new_plan_and_execute_admission() -> None:
    from app.routers.migrations import require_migration_admission

    # require_migration_admission is declared on the route itself, so a route found below a
    # FastAPI 0.141 nested include still carries it in its own dependant.
    routes = {
        (path, method): route
        for path, route in _walk_route_paths(create_app())
        for method in (getattr(route, "methods", None) or set())
    }

    def dependencies(path: str, method: str) -> set[object]:
        return {
            dependency.call
            for dependency in routes[(path, method)].dependant.dependencies
            if dependency.call is not None
        }

    assert require_migration_admission in dependencies(
        "/api/v1/migrations/plans", "POST"
    )
    assert require_migration_admission in dependencies(
        "/api/v1/migrations/plans/{plan_id}/execute", "POST"
    )
    assert require_migration_admission not in dependencies(
        "/api/v1/migrations/{job_id}", "GET"
    )
    assert require_migration_admission not in dependencies(
        "/api/v1/migrations/{job_id}/cleanup", "POST"
    )
