"""The unknown streak counts inconclusive outcomes since the job last made progress.

Counting every unknown over a job's life would stop a large project relocation in manual recovery
after a few separate, resolved blips. Resetting on any claim would let a target start that keeps
failing loop forever, since that loop changes nothing in the journal. Progress is therefore read
from the journal: resource states and the markers they record.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.migrations import worker
from app.services.migrations.reconciliation import _reconciliation_backoff_values, journal_progress


def _resource(rid: str, state: str, **marks: object) -> SimpleNamespace:
    return SimpleNamespace(resource_id=rid, state=state, safe_result=dict(marks))


def _job(**state: object) -> SimpleNamespace:
    return SimpleNamespace(job_id="job-1", safe_result=dict(state))


def test_progress_between_two_unknowns_starts_the_count_again() -> None:
    before = journal_progress([_resource("a", "syncing"), _resource("b", "pending")])
    after = journal_progress([_resource("a", "completed", transfer_reconciled=True), _resource("b", "syncing")])
    job = _job(reconciliation_streak=4, reconciliation_progress=before)

    values = _reconciliation_backoff_values(job, error_code="x", error_message="y", progress=after)

    assert values["status"] == "unknown"
    assert values["safe_result"]["reconciliation_streak"] == 1
    assert values["safe_result"]["reconciliation_progress"] == after


def test_unknowns_with_nothing_done_in_between_still_reach_manual_recovery() -> None:
    journal = [_resource("a", "cutting_over", target_start_attempted=True, target_identity_hash="h")]
    progress = journal_progress(journal)
    state: dict[str, object] = {}
    for _ in range(5):
        values = _reconciliation_backoff_values(_job(**state), error_code="x", error_message="y",
                                                progress=journal_progress(journal))
        state = values["safe_result"]

    assert values["status"] == "manual_recovery"
    assert state["reconciliation_progress"] == progress


def test_a_job_without_a_recorded_progress_keeps_its_count() -> None:
    values = _reconciliation_backoff_values(_job(reconciliation_streak=2), error_code="x", error_message="y",
                                            progress="abc")

    assert values["safe_result"]["reconciliation_streak"] == 3


def test_the_journal_value_ignores_operation_refs_and_order() -> None:
    one = journal_progress([_resource("b", "ready", a=1), _resource("a", "ready")])
    two = journal_progress([_resource("a", "ready"), _resource("b", "ready", a=1)])
    assert one == two


@pytest.mark.asyncio
async def test_the_worker_backs_off_with_the_journal_it_reads_now(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def progress(_job: object) -> str:
        return "after"

    async def fenced(_job: object, _worker_id: str, **values: object) -> bool:
        captured.update(values)
        return True

    monkeypatch.setattr(worker, "current_progress", progress)
    monkeypatch.setattr(worker, "_fenced_job_update", fenced)
    job = SimpleNamespace(job_id="job-1", phase="final_sync", scope="project",
                          safe_result={"reconciliation_streak": 4, "reconciliation_progress": "before"})

    await worker._back_off_unknown(job, "worker-a", RuntimeError("blip"))

    assert captured["status"] == "unknown"
    assert captured["safe_result"]["reconciliation_streak"] == 1  # type: ignore[index]


def test_a_journal_that_swings_back_and_forth_does_not_reset_the_count() -> None:
    # A step that fails (A), an inconclusive reconcile (B), then the same failure (A) again would
    # reset on every pass if any change counted as progress.
    state: dict[str, object] = {}
    values: dict[str, object] = {}
    for progress in ("A", "B", "A", "B", "A", "B"):
        values = _reconciliation_backoff_values(_job(**state), error_code="x", error_message="y", progress=progress)
        state = values["safe_result"]  # type: ignore[assignment]

    assert values["status"] == "manual_recovery"


def test_a_cycle_has_a_hard_cap_however_much_progress_it_reports() -> None:
    state: dict[str, object] = {}
    values: dict[str, object] = {}
    for index in range(40):
        values = _reconciliation_backoff_values(_job(**state), error_code="x", error_message="y", progress=f"p{index}")
        state = values["safe_result"]  # type: ignore[assignment]
        if values["status"] == "manual_recovery":
            break

    assert values["status"] == "manual_recovery"
    assert state["reconciliation_cycle_unknowns"] <= 25  # type: ignore[operator]


def test_recheck_starts_a_new_cycle() -> None:
    from app.services.migrations.actions import _reset_reconciliation_cycle

    reset = _reset_reconciliation_cycle(
        {"reconciliation_streak": 3, "reconciliation_seen_progress": ["A"], "reconciliation_cycle_unknowns": 9}
    )

    assert "reconciliation_seen_progress" not in reset and "reconciliation_cycle_unknowns" not in reset
