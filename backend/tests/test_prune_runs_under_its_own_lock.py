"""Every worker runs the prune loop; one transaction-level advisory lock decides who prunes.

`pg_try_advisory_xact_lock(20330, 21579)` is taken inside the pass's own transaction, so a
worker that dies mid-delete releases it with its transaction and the next pass on any worker
picks up. A worker that does not get the lock skips the pass without writing. A pass that does
the work records one `task.journal.prune` audit line with the counts.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

import pytest  # noqa: E402

from app.services.task_journal import prune as prune_module  # noqa: E402
from app.services.task_journal.repository_types import FOLLOWER_LOCK, PRUNE_LOCK  # noqa: E402
from tests._journal_fakes import FakeResult, FakeSessionMaker  # noqa: E402


@pytest.fixture
def audits(monkeypatch):
    recorded = []
    monkeypatch.setattr(prune_module, "_audit", lambda **details: recorded.append(details))
    return recorded


def _maker(lock_granted: bool, deleted: int = 0):
    def respond(sql, params):
        if "pg_try_advisory_xact_lock" in sql:
            return FakeResult(scalar=lock_granted)
        if sql.startswith("DELETE FROM"):
            return FakeResult(rowcount=deleted)
        return None

    return FakeSessionMaker(respond)


async def test_the_pass_takes_the_prune_lock_inside_its_transaction_and_prunes(audits):
    maker = _maker(True, deleted=3)

    result = await prune_module.prune_once(maker)

    db = maker.sessions[0]
    sql, params = db.calls[0]
    assert "SELECT pg_try_advisory_xact_lock(:classid, :objid)" in sql
    assert params == {"classid": PRUNE_LOCK[0], "objid": PRUNE_LOCK[1]} == {"classid": 20330, "objid": 21579}
    assert PRUNE_LOCK != FOLLOWER_LOCK, "its own lock, not the follower's"
    deletes = [s for s in db.statements if s.startswith("DELETE FROM")]
    assert any("cluster_log" in s for s in deletes) and any("task_journal" in s for s in deletes)
    assert db.commits == 1 and db.rollbacks == 0
    assert result == (3, 3)
    assert audits == [{"tasks": 3, "logs": 3, "task_days": 30, "log_days": 30}]


async def test_a_worker_without_the_lock_skips_the_pass_and_writes_nothing(audits):
    maker = _maker(False)

    assert await prune_module.prune_once(maker) is None

    db = maker.sessions[0]
    assert len(db.calls) == 1, "only the lock probe"
    assert db.commits == 0 and db.rollbacks == 1
    assert audits == []


async def test_the_retention_days_come_from_settings(monkeypatch, audits):
    maker = _maker(True)
    settings = prune_module.get_settings()
    monkeypatch.setattr(settings, "task_journal_retention_days", 7)
    monkeypatch.setattr(settings, "cluster_log_retention_days", 14)
    monkeypatch.setattr(prune_module, "get_settings", lambda: settings)

    await prune_module.prune_once(maker)

    days = [p["days"] for sql, p in maker.sessions[0].calls if p and "days" in p]
    assert 7 in days and 14 in days
    assert audits[0]["task_days"] == 7 and audits[0]["log_days"] == 14


def test_the_loop_starts_five_minutes_after_boot_then_every_six_hours_with_jitter():
    import inspect

    assert prune_module.FIRST_RUN_DELAY_S == 300.0
    assert prune_module.INTERVAL_S == 6 * 3600.0
    for _ in range(20):
        delay = prune_module._next_delay()
        assert prune_module.INTERVAL_S <= delay <= prune_module.INTERVAL_S + prune_module.JITTER_S
    source = inspect.getsource(prune_module.prune_loop)
    assert source.index("FIRST_RUN_DELAY_S") < source.index("prune_once(")
    assert "_next_delay()" in source


async def test_start_and_stop_keep_one_task():
    maker = _maker(True)
    prune_module.start_prune_loop(maker)
    first = prune_module._task
    prune_module.start_prune_loop(maker)
    assert prune_module._task is first, "a second start does not spawn a second loop"
    prune_module.stop_prune_loop()
    assert prune_module._task is None
    try:
        await first
    except BaseException:  # noqa: BLE001 - cancelled, as intended
        pass
    assert first.cancelled()
