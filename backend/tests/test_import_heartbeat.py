"""The import heartbeat has to survive a transient database error.

While an import runs, `_heartbeat` bumps `updated_at` so the recovery sweep can tell a live
import from one whose worker was killed. If one database error ended the loop, `updated_at`
would go stale, the sweep would re-drive the running import, and the two runs would race: the
winner's `finally: _cleanup_file(...)` pulls the staging file out from under the loser, which
then writes `status='failed'` over a row the winner already set to `'ready'`.

Both upload routers carry the same loop, so both are asserted here.
"""

import asyncio
from contextlib import asynccontextmanager
from typing import List

import pytest

from app.routers import image_uploads, iso_uploads

MODULES = [
    pytest.param(image_uploads, id="image_uploads"),
    pytest.param(iso_uploads, id="iso_uploads"),
]


class _FakeSession:
    def __init__(self, attempts: List[str]) -> None:
        self._attempts = attempts

    async def execute(self, *_args, **_kwargs) -> None:
        return None

    async def commit(self) -> None:
        self._attempts.append("ok")


def _session_maker_failing_once(attempts: List[str]):
    """A session factory that raises on its first use and works from then on."""
    state = {"first": True}

    @asynccontextmanager
    async def maker():
        if state["first"]:
            state["first"] = False
            attempts.append("boom")
            raise RuntimeError("connection reset by peer")
        yield _FakeSession(attempts)

    return maker


@pytest.mark.parametrize("module", MODULES)
async def test_heartbeat_keeps_beating_after_a_transient_db_error(module, monkeypatch) -> None:
    attempts: List[str] = []
    monkeypatch.setattr(module, "_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(module, "async_session_maker", _session_maker_failing_once(attempts))

    task = asyncio.create_task(module._heartbeat("session-under-test"))
    await asyncio.sleep(0.25)
    still_running = not task.done()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert attempts[0] == "boom", "the test did not exercise the failure path"
    assert still_running, "the heartbeat ended on the first database error"
    assert attempts.count("ok") >= 1, "the heartbeat never recovered after the error"


@pytest.mark.parametrize("module", MODULES)
async def test_heartbeat_still_exits_on_cancellation(module, monkeypatch) -> None:
    """Keeping the loop alive must not make the task uncancellable at shutdown."""
    attempts: List[str] = []
    monkeypatch.setattr(module, "_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(module, "async_session_maker", _session_maker_failing_once(attempts))

    task = asyncio.create_task(module._heartbeat("session-under-test"))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=2.0)

    assert task.done()
