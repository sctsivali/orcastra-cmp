"""A target start that keeps failing reaches manual recovery instead of looping.

reconcile_staged keeps reconciliation_streak when it moves a job with adopted targets back to
target_validating. Resetting it would count each failed start as the first unknown again, so
the five-attempt cap would never fire while the source stays fenced and stopped.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.services.migrations import reconcile_transfer


def _adopted(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        resource_id=f"r-{name}",
        resource_type="instance",
        source_ref={"name": name},
        target_ref={"name": name},
        safe_result={
            "transfer_submission_intent": True,
            "contract_hash": "c",
            "target_identity_hash": "i",
            "target_start_attempted": True,
        },
    )


@pytest.mark.asyncio
async def test_resuming_already_adopted_targets_keeps_the_streak(monkeypatch: pytest.MonkeyPatch) -> None:
    job = SimpleNamespace(
        job_id="job-1", scope="instance", strategy="cross_cluster_pull", target_project="dst",
        safe_result={"reconciliation_streak": 3},
    )
    updates: list[dict[str, Any]] = []

    async def job_plan(_job: Any) -> Any:
        return SimpleNamespace(
            preflight_payload={},
            request_payload={
                "scope": "instance", "intent": "move",
                "source": {"cluster_id": "a", "project": "src", "resource_name": "web"},
                "target": {"cluster_id": "b", "project": "dst", "resource_name": "web"},
            },
        )

    async def fenced(_job: Any, _worker: str, **values: Any) -> bool:
        updates.append(values)
        return True

    monkeypatch.setattr(reconcile_transfer, "_job_plan", job_plan)
    monkeypatch.setattr(reconcile_transfer, "_fenced_job_update", fenced)

    handled = await reconcile_transfer.reconcile_staged(
        job, "worker-a", object(), object(), resources=[_adopted("web")]
    )

    assert handled is True
    assert updates[-1]["phase"] == "target_validating"
    assert updates[-1]["safe_result"]["reconciliation_streak"] == 3
