"""A source whose stop never took effect ends the migration cleanly instead of parking it.

Found live on the lab: a VM whose guest ignored ACPI shutdown outlasted the stop timeout. The
source stayed running, nothing was transferred, and yet the job sat in `unknown` until it would
have escalated to `manual_recovery`. Reconciliation now recognises "the source is exactly the
approved running workload and no transfer was submitted" and routes the job to the abort runner.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.services.migrations import executor, reconciliation

SOURCE = {
    "name": "vm",
    "type": "virtual-machine",
    "architecture": "x86_64",
    "status": "Running",
    "profiles": ["default"],
    "config": {"volatile.uuid": "uuid-1", "limits.cpu": "1"},
    "devices": {},
    "expanded_config": {"volatile.uuid": "uuid-1", "limits.cpu": "1"},
    "expanded_devices": {"root": {"type": "disk", "path": "/", "pool": "default"}},
}


def _resource(**state: Any) -> Any:
    base = {
        "original_status": "running",
        "stop_submission_intent": True,
        "stop_submission_at": "2026-10-15T17:03:51+00:00",
        "source_identity_hash": executor.instance_identity_fingerprint(SOURCE),
        "source_definition_hash": executor.instance_fingerprint(SOURCE),
    }
    base.update(state)
    return SimpleNamespace(
        resource_id="r1", resource_type="instance", source_ref={"name": "vm"}, safe_result=base
    )


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    recorded: dict[str, list[Any]] = {"job": [], "unknown": [], "resource": []}

    async def direct(function: Any, *args: Any, **kwargs: Any) -> Any:
        kwargs.pop("label", None)
        return function(*args, **kwargs)

    async def job_update(_job: Any, _worker: str, **values: Any) -> bool:
        recorded["job"].append(values)
        return True

    async def unknown(_job: Any, _worker: str, **values: Any) -> None:
        recorded["unknown"].append(values)

    async def resource_update(_resource_id: str, **values: Any) -> None:
        recorded["resource"].append(values)

    monkeypatch.setattr(reconciliation, "_blocking", direct)
    monkeypatch.setattr(reconciliation, "_fenced_job_update", job_update)
    monkeypatch.setattr(reconciliation, "_mark_reconciliation_unknown", unknown)
    monkeypatch.setattr(reconciliation, "_resource_update", resource_update)
    monkeypatch.setattr(reconciliation.executor, "read_instance", lambda *_a, **_k: dict(SOURCE))
    return recorded


JOB = SimpleNamespace(
    job_id="job-1", source_project="src", source_cluster_id="a", safe_result={"reason": "x"}
)


def _evidence(monkeypatch: pytest.MonkeyPatch, **values: Any) -> None:
    async def evidence(*_args: Any, **_kwargs: Any) -> Any:
        return reconciliation._OperationEvidence(**values)

    monkeypatch.setattr(reconciliation, "_operation_evidence", evidence)


@pytest.mark.parametrize(
    "evidence",
    [{}, {"seen": True, "settled": True, "failed": True}, {"seen": True, "forgotten": True}],
)
async def test_an_unchanged_running_source_routes_the_job_to_abort(
    monkeypatch: pytest.MonkeyPatch, calls: dict[str, list[Any]], evidence: dict[str, Any]
) -> None:
    _evidence(monkeypatch, **evidence)

    handled = await reconciliation._reconcile_pretransfer(JOB, "w", object(), [_resource()])

    assert handled is True
    assert calls["unknown"] == []
    [update] = calls["job"]
    assert update["phase"] == "rolling_back"
    assert update["safe_result"]["recovery_kind"] == "abort"
    assert update["safe_result"]["abort_code"] == "source_stop_failed"


async def test_a_stop_still_in_flight_waits(
    monkeypatch: pytest.MonkeyPatch, calls: dict[str, list[Any]]
) -> None:
    _evidence(monkeypatch, seen=True, settled=False)

    await reconciliation._reconcile_pretransfer(JOB, "w", object(), [_resource()])

    assert calls["job"] == []
    assert len(calls["unknown"]) == 1


async def test_a_transfer_intent_keeps_the_old_rule(
    monkeypatch: pytest.MonkeyPatch, calls: dict[str, list[Any]]
) -> None:
    _evidence(monkeypatch)

    handled = await reconciliation._reconcile_pretransfer(
        JOB, "w", object(), [_resource(transfer_submission_intent=True)]
    )

    assert handled is False
    assert calls["job"] == []


async def test_a_changed_source_is_not_treated_as_untouched(
    monkeypatch: pytest.MonkeyPatch, calls: dict[str, list[Any]]
) -> None:
    _evidence(monkeypatch)
    changed = dict(SOURCE, config={"volatile.uuid": "uuid-1", "limits.cpu": "4"},
                   expanded_config={"volatile.uuid": "uuid-1", "limits.cpu": "4"})
    monkeypatch.setattr(reconciliation.executor, "read_instance", lambda *_a, **_k: changed)

    await reconciliation._reconcile_pretransfer(JOB, "w", object(), [_resource()])

    assert calls["job"] == []
    assert len(calls["unknown"]) == 1


async def test_an_abort_records_its_real_reason_not_the_generic_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Found live: a VM stop timeout ended as "Transfer failed." because the abort runner read
    the reason from the pre-abort copy of the job."""
    from app.services.migrations import rollback
    from app.services.migrations.contracts import TransferFailed

    seen: dict[str, Any] = {}

    async def job_update(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def run_abort(job: Any, *_args: Any) -> None:
        seen.update(job.safe_result)

    monkeypatch.setattr(rollback, "_fenced_job_update", job_update)
    monkeypatch.setattr(rollback, "run_abort", run_abort)
    job = SimpleNamespace(job_id="j", safe_result={"reason": "x"})

    await rollback.abort_before_activation(
        job, "w", object(), object(),
        reason=TransferFailed("Source 'vm' did not shut down within 10 seconds: context deadline exceeded."),
    )

    assert seen["recovery_kind"] == "abort"
    assert "did not shut down within 10 seconds" in seen["abort_reason"]
