"""A job that finishes cleanly does not keep the error of a transient retry on the way.

Found live: an authority retry during a member relocation left
`authentik_temporarily_unavailable` on a job that then completed with success, so the console
showed an error on a successful job.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.services.migrations import context


class _Session:
    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def set_job(_db: Any, _job_id: str, **values: Any) -> bool:
        calls.append(values)
        return True

    monkeypatch.setattr(context, "async_session_maker", _Session)
    monkeypatch.setattr(context.repository, "set_job", set_job)
    monkeypatch.setattr(context.repository, "event_actor", lambda _values: {})
    return calls


JOB = SimpleNamespace(
    job_id="job-1",
    fence_token=1,
    requester_role="partner",
    actor_issuer=None,
    actor_subject=None,
    actor_email=None,
    actor_provider_uuid=None,
)


async def test_a_successful_transition_clears_a_stale_error(captured: list[dict[str, Any]]) -> None:
    await context._fenced_job_update(JOB, "worker", phase="completed", status="success", event_message="done")

    assert captured[0]["error_code"] is None
    assert captured[0]["error_message"] is None


async def test_other_transitions_keep_what_they_set(captured: list[dict[str, Any]]) -> None:
    await context._fenced_job_update(
        JOB, "worker", phase="manual_recovery", status="manual_recovery",
        error_code="x", error_message="y", event_message="stuck",
    )
    await context._fenced_job_update(JOB, "worker", phase="initial_sync", status="running", event_message="go")

    assert captured[0]["error_code"] == "x"
    assert "error_code" not in captured[1]
