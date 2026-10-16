"""Project jobs go to their own convergent runners, and only relocations offer retention actions."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.models.migration_schemas import MigrationActionName
from app.services.migrations import billing, presenters, worker
from app.services.migrations.actions import _has_retained_source


def _job(**values: Any) -> SimpleNamespace:
    base = {
        "job_id": "job-1",
        "scope": "project",
        "intent": "move",
        "strategy": "project_relocation",
        "phase": "final_sync",
        "status": "running",
        "safe_result": {},
        "lease_expires_at": None,
    }
    base.update(values)
    return SimpleNamespace(**base)


@pytest.fixture
def ran(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def runner(name: str) -> Any:
        async def run(*_args: Any, **_kwargs: Any) -> None:
            calls.append(name)

        return run

    from app.services.migrations import (
        project_cleanup,
        project_member_relocation,
        project_relocation,
        project_rollback,
    )

    monkeypatch.setattr(project_cleanup, "run_project_cleanup", runner("cleanup"))
    monkeypatch.setattr(project_member_relocation, "relocate_members", runner("members"))
    monkeypatch.setattr(project_relocation, "advance", runner("advance"))
    monkeypatch.setattr(project_rollback, "run_project_abort", runner("abort"))
    monkeypatch.setattr(project_rollback, "run_project_rollback", runner("rollback"))
    return calls


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({"phase": "final_sync"}, "advance"),
        ({"phase": "cleaning_up"}, "cleanup"),
        ({"phase": "rolling_back", "safe_result": {"recovery_kind": "abort"}}, "abort"),
        ({"phase": "rolling_back", "safe_result": {"recovery_kind": "rollback"}}, "rollback"),
        ({"phase": "final_sync", "strategy": "member_relocation"}, "members"),
        ({"phase": "rolling_back", "strategy": "member_relocation"}, "members"),
        # A recheck after a recovery runner stopped resumes that runner, never the forward path.
        ({"phase": "reconciling", "safe_result": {"recovery_kind": "abort"}}, "abort"),
        ({"phase": "reconciling", "safe_result": {"recovery_kind": "rollback"}}, "rollback"),
        ({"phase": "reconciling", "safe_result": {"recovery_kind": "cleanup"}}, "cleanup"),
        ({"phase": "reconciling"}, "advance"),
    ],
)
async def test_each_project_phase_resumes_its_own_runner(ran: list[str], values: dict[str, Any], expected: str) -> None:
    await worker._run_project(_job(**values), "worker", object(), object())

    assert ran == [expected]


def test_only_a_relocation_or_a_staged_move_keeps_a_source() -> None:
    assert _has_retained_source(_job())
    assert _has_retained_source(_job(scope="instance", strategy="cross_cluster_pull"))
    assert not _has_retained_source(_job(strategy="member_relocation"))
    assert not _has_retained_source(_job(scope="instance", intent="copy", strategy="same_cluster_copy"))


@pytest.mark.parametrize(("strategy", "offered"), [("project_relocation", True), ("member_relocation", False)])
def test_a_retained_project_offers_rollback_and_finalize(strategy: str, offered: bool) -> None:
    job = _job(strategy=strategy, phase="source_retained", status="waiting")

    actions = presenters._available_actions(job, [])

    assert (MigrationActionName.ROLLBACK in actions) is offered
    assert (MigrationActionName.CLEANUP in actions) is offered


def test_a_relocation_asks_to_acknowledge_deleting_the_source_project() -> None:
    required = billing.required_acknowledgements(
        intent="move", strategy="project_relocation", external_cutover_required=False
    )

    assert {"source-project-deletion-understood", "source-retention-understood"} <= required
    assert "source-project-deletion-understood" not in billing.required_acknowledgements(
        intent="move", strategy="member_relocation", external_cutover_required=False
    )
