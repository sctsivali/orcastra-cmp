"""The writer drains in batches, retries a refused batch three times, then lets it go.

A database that is briefly away must not stall every worker's queue behind one batch, and a
database that is away for good must not hold five thousand records in memory forever. One
attempt, three retries at 0.5, 1 and 2 seconds, then an error line that says how many records
were not journaled and where the record of truth still is. A record that cannot even be mapped is
skipped on its own, so one malformed audit call never blocks the ones behind it.
"""

import asyncio
import logging

import pytest

from app.services.task_journal import sink as sink_module
from app.services.task_journal.sink import JournalSink
from tests._journal_fakes import FakeSessionMaker, audit_record


@pytest.fixture(autouse=True)
def _no_org(monkeypatch):
    async def none(host, db):
        return (None, None)

    monkeypatch.setattr("app.core.org_scope.resolve_cluster_org", none)


@pytest.fixture
def sleeps(monkeypatch):
    """Records every delay the sink asks for and returns at once."""
    recorded = []

    async def fake_sleep(delay):
        recorded.append(delay)

    monkeypatch.setattr(sink_module.asyncio, "sleep", fake_sleep)
    return recorded


def _sink(maker):
    sink = JournalSink()
    sink._session_maker = maker
    return sink


@pytest.mark.asyncio
async def test_three_refusals_then_a_write_follow_the_half_one_two_schedule(sleeps):
    maker = FakeSessionMaker(fail_times=3)
    await _sink(maker)._write_with_retry([audit_record()])
    assert sleeps == [0.5, 1.0, 2.0]
    assert maker.opened == 4
    assert any(sql.startswith("INSERT INTO cluster_log") for sql in maker.statements)


@pytest.mark.asyncio
async def test_four_refusals_drop_the_batch_with_the_error_line(sleeps, caplog):
    maker = FakeSessionMaker(fail_times=4)
    batch = [audit_record(), audit_record("instance.stop")]
    with caplog.at_level(logging.ERROR, logger=sink_module.__name__):
        await _sink(maker)._write_with_retry(batch)
    assert sleeps == [0.5, 1.0, 2.0], "no sleep after the last attempt: the batch is gone, not queued"
    assert maker.opened == 4
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert errors == ["2 audit records not journaled; the stdout record is the audit of record"]


@pytest.mark.asyncio
async def test_a_record_that_cannot_be_mapped_is_skipped_and_the_rest_still_write(monkeypatch):
    real = sink_module.record_to_log_row

    def poisoned(record):
        if record.get("action") == "instance.stop":
            raise KeyError("poison")
        return real(record)

    monkeypatch.setattr(sink_module, "record_to_log_row", poisoned)
    maker = FakeSessionMaker()
    sink = _sink(maker)
    await sink._write_batch([audit_record("instance.stop"), audit_record("instance.start")])
    inserts = [p for sql, p in maker.sessions[0].calls if sql.startswith("INSERT INTO cluster_log")]
    assert len(inserts) == 1
    assert [row["action"] for row in inserts[0]] == ["instance.start"]
    assert sink.written == 1


@pytest.mark.asyncio
async def test_the_writer_loop_survives_a_poison_batch_and_writes_the_next(monkeypatch, caplog):
    real = sink_module.record_to_log_row

    def poisoned(record):
        if record.get("action") == "instance.stop":
            raise KeyError("poison")
        return real(record)

    monkeypatch.setattr(sink_module, "record_to_log_row", poisoned)
    monkeypatch.setattr(sink_module, "record_to_task", lambda record: None)
    maker = FakeSessionMaker()
    sink = JournalSink()
    sink.start(asyncio.get_running_loop(), maker)
    try:
        sink._enqueue(audit_record("instance.stop"))
        for _ in range(50):
            await asyncio.sleep(0.02)
            if sink._task.done():
                break
        assert not sink._task.done(), "the writer must outlive a record it cannot map"
        sink._enqueue(audit_record("instance.start"))
        for _ in range(100):
            if maker.statements:
                break
            await asyncio.sleep(0.02)
        assert any(sql.startswith("INSERT INTO cluster_log") for sql in maker.statements)
    finally:
        await sink.stop()


@pytest.mark.asyncio
async def test_a_batch_closes_at_two_hundred_items_or_the_window():
    sink = JournalSink()
    sink._queue = asyncio.Queue(maxsize=sink_module.QUEUE_MAX)
    for i in range(250):
        sink._queue.put_nowait({"i": i})
    first = await sink._next_batch()
    assert len(first) == sink_module.BATCH_MAX == 200
    second = await sink._next_batch()
    assert len(second) == 50, "the window closes the batch once the queue runs dry"
    assert sink._queue.empty()


@pytest.mark.asyncio
async def test_a_batch_that_maps_to_nothing_opens_no_session():
    maker = FakeSessionMaker()
    sink = _sink(maker)
    await sink._write_batch([audit_record("lxd.instance.start"), audit_record("audit.view")])
    assert maker.opened == 0


@pytest.mark.asyncio
async def test_one_transaction_per_batch():
    maker = FakeSessionMaker()
    sink = _sink(maker)
    await sink._write_batch([audit_record("instance.start"), audit_record("instance.stop")])
    assert maker.opened == 1
    db = maker.sessions[0]
    assert db.commits == 1
    inserts = [p for sql, p in db.calls if sql.startswith("INSERT INTO cluster_log")]
    assert len(inserts) == 1 and len(inserts[0]) == 2, "one executemany for the batch's log rows"
    assert db.statements[-1].startswith("INSERT INTO cluster_log"), "log rows land after their tasks"
