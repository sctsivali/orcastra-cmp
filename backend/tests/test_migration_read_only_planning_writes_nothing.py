"""Read-only mode keeps planning available but writes nothing, the certificate store included.

Planning recorded a missing certificate pin (trust on first use) or a renewal as a side effect,
including in read-only mode, so a partner could choose the moment a cluster's identity was first
trusted while the deployment was frozen. PATCH also skipped the Migration Center switch.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.routers import migrations as migrations_router
from app.services.lxd_certs.pinning import TRUST_TOFU, PinState
from app.services.migrations import planning
from app.services.migrations.contracts import MigrationConflict

PIN = "a" * 64


class _Planner(planning.MigrationPlanningMixin):
    def __init__(self, *, read_only: bool) -> None:
        self.settings = SimpleNamespace(read_only_mode=read_only)
        self.lxd_service = object()


def _world(monkeypatch: pytest.MonkeyPatch, pinned: bool) -> list[str]:
    refreshed: list[str] = []

    async def pin_state(_cluster_id: str, *, strict: bool = False) -> Any:
        return PinState(fingerprint=PIN, trust=TRUST_TOFU, host=None, previous=()) if pinned else None

    async def refresh(_service: Any, cluster_id: str) -> None:
        refreshed.append(cluster_id)

    monkeypatch.setattr(planning, "cluster_pin_state", pin_state)
    monkeypatch.setattr(planning, "refresh_pin_for", refresh)
    return refreshed


@pytest.mark.asyncio
async def test_read_only_planning_uses_the_recorded_pin_without_writing(monkeypatch: pytest.MonkeyPatch) -> None:
    refreshed = _world(monkeypatch, pinned=True)

    state = await _Planner(read_only=True)._pin_state("cluster-a")

    assert state.fingerprint == PIN
    assert refreshed == []


@pytest.mark.asyncio
async def test_read_only_planning_refuses_a_cluster_that_was_never_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    refreshed = _world(monkeypatch, pinned=False)

    with pytest.raises(MigrationConflict, match="read-only"):
        await _Planner(read_only=True)._pin_state("cluster-a")

    assert refreshed == []


@pytest.mark.asyncio
async def test_normal_planning_still_records_a_missing_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    refreshed = _world(monkeypatch, pinned=True)

    await _Planner(read_only=False)._pin_state("cluster-a")

    assert refreshed == ["cluster-a"]


def test_patching_a_plan_honours_the_migration_center_switch() -> None:
    route = next(
        r for r in migrations_router.router.routes
        if getattr(r, "path", "").endswith("/plans/{plan_id}") and "PATCH" in getattr(r, "methods", set())
    )
    dependencies = {getattr(d.call, "__name__", "") for d in route.dependant.dependencies}
    assert "require_migration_admission" in dependencies
