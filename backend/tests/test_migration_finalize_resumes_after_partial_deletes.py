"""A Finalize that resumes after it already deleted something finishes or stops, never loops.

* The source project is already gone (the delete timed out but LXD finished it, or the worker
  died after it): the cleanup finishes instead of failing every Recheck on the missing
  project's inventory, so the deleted events and the grant revoke still run.
* Some sources are deleted and something else drifted: the job stops for manual recovery,
  since a Rollback from retention cannot restore what is gone.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.services.migrations import project_cleanup, project_rollback


def _instance(name: str, **state: Any) -> SimpleNamespace:
    return SimpleNamespace(
        resource_id=f"r-{name}", resource_type="instance", ordinal=len(name), source_ref={"name": name},
        target_ref={"name": name}, safe_result=state, state="cleaning",
    )


def _job() -> SimpleNamespace:
    return SimpleNamespace(
        job_id="job-1", source_project="web", target_project="web", source_cluster_id="a",
        target_cluster_id="b", safe_result={"cleanup_trigger": "finalize"}, fence_token=1,
        requester_role="partner", actor_issuer="iss", actor_subject="sub",
        actor_email="ops@example.test", actor_provider_uuid="uuid",
    )


def _wire(monkeypatch: pytest.MonkeyPatch, resources: list[Any], *, project_exists: bool, drift: Any) -> dict[str, Any]:
    seen: dict[str, Any] = {"finished": False, "refused": None, "manual": None, "completed": []}

    async def fence(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def listed(_job_id: str) -> list[Any]:
        return resources

    async def blocking(fn: Any, *args: Any, **kwargs: Any) -> Any:
        if fn.__name__ == "read_project":
            return {"name": "web", "config": {}} if project_exists else None
        raise AssertionError(f"unexpected daemon call {fn.__name__}")

    async def drift_check(*_args: Any, **_kwargs: Any) -> Any:
        return drift

    async def refuse(_job: Any, _worker: str, code: str, message: str) -> None:
        seen["refused"] = code

    async def manual(_job: Any, _worker: str, *, code: str, message: str) -> None:
        seen["manual"] = code

    async def resource_update(resource_id: str, **values: Any) -> None:
        if values.get("state") == "completed":
            seen["completed"].append(resource_id)

    class _Session:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_exc: Any) -> None:
            return None

    async def finish(*_args: Any, **_kwargs: Any) -> None:
        seen["finished"] = True

    monkeypatch.setattr(project_cleanup, "fence_or_reason", fence)
    monkeypatch.setattr(project_cleanup, "_resources", listed)
    monkeypatch.setattr(project_cleanup, "_blocking", blocking)
    monkeypatch.setattr(project_cleanup, "_drift", drift_check)
    monkeypatch.setattr(project_cleanup, "_refuse", refuse)
    monkeypatch.setattr(project_cleanup, "_manual_recovery", manual)
    monkeypatch.setattr(project_cleanup, "_resource_update", resource_update)
    monkeypatch.setattr(project_cleanup, "async_session_maker", lambda: _Session())
    monkeypatch.setattr(project_cleanup, "finish_project_cleanup", finish)
    monkeypatch.setattr(project_cleanup, "project_instance_events", lambda *_a, **_k: [])
    return seen


@pytest.mark.asyncio
async def test_a_source_project_already_deleted_finishes_the_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    resources = [_instance("db", cleanup_submission_intent=True), _instance("web", cleanup_submission_intent=True)]
    seen = _wire(monkeypatch, resources, project_exists=False, drift=("source_project_changed", "unreadable"))

    await project_cleanup.run_project_cleanup(_job(), "worker-a", object(), object())

    assert seen["finished"] is True
    assert seen["refused"] is None and seen["manual"] is None
    assert set(seen["completed"]) == {"r-db", "r-web"}


@pytest.mark.asyncio
async def test_drift_after_a_source_was_deleted_stops_for_manual_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    resources = [_instance("db", cleanup_submission_intent=True), _instance("web")]
    seen = _wire(monkeypatch, resources, project_exists=True, drift=("source_not_fenced", "web was started"))

    await project_cleanup.run_project_cleanup(_job(), "worker-a", object(), object())

    assert seen["manual"] == "source_not_fenced"
    assert seen["refused"] is None
    assert seen["finished"] is False


@pytest.mark.asyncio
async def test_drift_before_any_delete_still_returns_to_retention(monkeypatch: pytest.MonkeyPatch) -> None:
    resources = [_instance("db"), _instance("web")]
    seen = _wire(monkeypatch, resources, project_exists=True, drift=("source_not_fenced", "web was started"))

    await project_cleanup.run_project_cleanup(_job(), "worker-a", object(), object())

    assert seen["refused"] == "source_not_fenced"


@pytest.mark.asyncio
async def test_a_project_delete_that_timed_out_but_landed_counts_as_done(monkeypatch: pytest.MonkeyPatch) -> None:
    import requests

    reads = iter([{"name": "web", "config": {}}, None])

    async def blocking(fn: Any, *args: Any, **kwargs: Any) -> Any:
        if fn.__name__ == "read_project":
            return next(reads)
        if fn.__name__ == "delete_project":
            raise requests.exceptions.ReadTimeout("read timed out")
        raise AssertionError(fn.__name__)

    async def lease() -> None:
        return None

    monkeypatch.setattr(project_rollback, "_blocking", blocking)
    monkeypatch.setattr(project_rollback, "_assert_mutation_lease", lease)
    monkeypatch.setattr(project_rollback, "features_enabled", lambda *_a: False)

    await project_rollback.delete_job_project(_job(), [], object(), side="source")
