"""The migration outbox cannot hold the recovery loop or hammer a receiver that is down.

The drain runs inside the recovery tick, so an unbounded drain against a black-holed receiver
would delay every queued job, retention cleanup and plan expiry, and rows outliving their lease
would be claimed and sent again. Each drain stops at a time budget and hands back the rest, and
failed deliveries back off exponentially.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.migrations import repository, worker


def test_failed_deliveries_back_off_exponentially_up_to_an_hour() -> None:
    delays = [repository.outbox_retry_delay(attempts) for attempts in (1, 2, 3, 6, 50)]

    assert delays[:3] == [timedelta(seconds=30), timedelta(seconds=60), timedelta(seconds=120)]
    assert delays[3] == timedelta(seconds=960)
    assert delays[4] == timedelta(hours=1)


class _Session:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_exc: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_a_drain_stops_at_its_budget_and_hands_back_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        SimpleNamespace(event_id=f"e{i}", job_id="job-1", channel="billing", payload={}, attempts=1)
        for i in range(5)
    ]
    clock = iter(range(0, 1000, 15))
    finished: list[str] = []
    released: list[str] = []

    async def pending(_db: Any, *, worker_id: str) -> list[Any]:
        return rows

    async def get_job(_db: Any, _job_id: str) -> None:
        return None

    async def deliver(_row: Any) -> bool:
        return True

    async def finish(_db: Any, event_id: str, **_kwargs: Any) -> None:
        finished.append(event_id)

    async def release(_db: Any, event_ids: list[str], *, worker_id: str) -> None:
        released.extend(event_ids)

    monkeypatch.setattr(worker, "async_session_maker", lambda: _Session())
    monkeypatch.setattr(worker.repository, "pending_outbox_rows", pending)
    monkeypatch.setattr(worker.repository, "get_job", get_job)
    monkeypatch.setattr(worker.repository, "finish_outbox", finish)
    monkeypatch.setattr(worker.repository, "release_outbox", release)
    monkeypatch.setattr(worker, "deliver_completion_event", deliver)
    monkeypatch.setattr(worker, "_monotonic", lambda: float(next(clock)))

    delivered = await worker.drain_migration_audits()

    assert delivered == len(finished) < len(rows)
    assert released == [row.event_id for row in rows[len(finished):]]
