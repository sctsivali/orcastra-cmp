"""Missing terminal events cannot grow the writer's progress timing memo without a bound."""

import asyncio
from types import SimpleNamespace

import pytest

from app.services.task_journal import follower_writer as writer_module
from app.services.task_journal.follower_writer import JournalWriter
from tests._follower_fakes import NoIdentities
from tests._journal_fakes import FakeSessionMaker


def _frame(op, *, host="cluster-a", progress="1%", status="Running", code=103):
    return {
        "type": "operation",
        "host": host,
        "operation_id": op,
        "progress": progress,
        "status": status,
        "status_code": code,
    }


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(writer_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


def _writer():
    return JournalWriter(FakeSessionMaker(), identities=NoIdentities())


def test_missing_terminal_events_leave_at_most_the_memo_budget(clock):
    writer = _writer()
    limit = writer_module._STATUS_MEMO_MAX

    for offset in range(0, limit + 201, writer_module.BATCH_MAX):
        batch = [
            _frame(f"orphan-{index}")
            for index in range(offset, min(offset + writer_module.BATCH_MAX, limit + 201))
        ]
        assert writer.coalesce(batch) == batch

    assert len(writer._last_status) == limit
    assert len(writer._progress_written_at) == limit
    assert ("cluster-a", "orphan-0") not in writer._progress_written_at
    assert ("cluster-a", f"orphan-{limit + 200}") in writer._progress_written_at


def test_eviction_keeps_recent_operations_throttled(clock, monkeypatch):
    monkeypatch.setattr(writer_module, "_STATUS_MEMO_MAX", 3)
    writer = _writer()
    writer.coalesce([_frame(f"op-{index}") for index in range(5)])

    clock[0] += 0.2
    recent = _frame("op-4", progress="2%")
    assert writer.coalesce([recent]) == []
    assert writer._pending_progress[("cluster-a", "op-4")] == recent
    assert set(writer._progress_written_at) == {
        ("cluster-a", "op-2"), ("cluster-a", "op-3"), ("cluster-a", "op-4"),
    }


def test_flushing_held_progress_also_obeys_the_memo_budget(clock, monkeypatch):
    monkeypatch.setattr(writer_module, "_STATUS_MEMO_MAX", 3)
    writer = _writer()
    held = _frame("op-0", progress="2%")
    writer.coalesce([_frame(f"op-{index}") for index in range(3)] + [held])
    assert writer._pending_progress == {("cluster-a", "op-0"): held}

    clock[0] += 0.2
    newest = _frame("op-3")
    assert writer.coalesce([newest]) == [newest, held]
    assert writer._pending_progress == {}
    assert len(writer._progress_written_at) == 3
    assert ("cluster-a", "op-3") in writer._progress_written_at


@pytest.mark.parametrize(
    "status, code", [("Success", 200), ("Failure", 400), ("Cancelled", 401), ("Lost", None)],
)
def test_a_terminal_frame_removes_only_its_operation_memos(clock, status, code):
    writer = _writer()
    keys = [("cluster-a", "op-1"), ("cluster-b", "op-1"), ("cluster-a", "op-2")]
    for host, op in keys:
        writer.coalesce([_frame(op, host=host), _frame(op, host=host, progress="2%")])

    terminal = _frame("op-1", status=status, code=code)
    assert writer.coalesce([terminal]) == [terminal]
    for memo in (writer._pending_progress, writer._progress_written_at, writer._last_status):
        assert set(memo) == set(keys[1:])


def test_an_unknown_terminal_frame_does_not_evict_an_open_operations_throttle(clock, monkeypatch):
    monkeypatch.setattr(writer_module, "_STATUS_MEMO_MAX", 3)
    writer = _writer()
    writer.coalesce([_frame(f"op-{index}") for index in range(3)])
    before_progress = dict(writer._progress_written_at)
    before_status = dict(writer._last_status)

    for status, code in [("Success", 200), ("Failure", 400), ("Cancelled", 401), ("Lost", None)]:
        terminal = _frame(f"unknown-{status}", status=status, code=code)
        assert writer.coalesce([terminal]) == [terminal]
        assert writer._progress_written_at == before_progress
        assert writer._last_status == before_status


def test_hosts_and_operation_ids_do_not_share_a_throttle(clock):
    writer = _writer()
    first = _frame("same")
    second = _frame("same", host="cluster-b")
    third = _frame("other")
    assert writer.coalesce([first]) == [first]

    clock[0] += 0.2
    assert writer.coalesce([second, third]) == [second, third]
    held = [
        _frame("same", progress="2%"),
        _frame("same", host="cluster-b", progress="2%"),
        _frame("other", progress="2%"),
    ]
    assert writer.coalesce(held) == []
    assert set(writer._pending_progress) == {
        ("cluster-a", "same"), ("cluster-b", "same"), ("cluster-a", "other"),
    }


async def test_stop_preserves_accepted_held_progress_for_the_next_writer_handoff(clock, monkeypatch):
    writer = _writer()
    held = _frame("op-1", progress="2%")
    writer.coalesce([_frame("op-1"), held])
    previous_task = asyncio.create_task(asyncio.Event().wait())
    writer._task = previous_task
    await asyncio.sleep(0)

    await writer.stop()

    assert previous_task.cancelled()
    assert writer._task is None
    assert writer._pending_progress == {("cluster-a", "op-1"): held}

    recorded = []
    written = asyncio.Event()
    next_batches = 0

    async def next_batch():
        nonlocal next_batches
        next_batches += 1
        if next_batches == 1:
            return []
        await asyncio.Event().wait()

    async def write_batch(frames):
        recorded.extend(frames)
        written.set()

    monkeypatch.setattr(writer, "_next_batch", next_batch)
    monkeypatch.setattr(writer, "write_batch", write_batch)
    clock[0] += writer_module.PROGRESS_INTERVAL_S
    writer.start()
    try:
        await asyncio.wait_for(written.wait(), timeout=1.0)
    finally:
        await writer.stop()

    assert recorded == [held]
    assert writer._pending_progress == {}
    terminal = _frame("op-1", status="Success", code=200)
    assert writer.coalesce([terminal]) == [terminal]
    assert writer._progress_written_at == {}
    assert writer._last_status == {}


async def test_cancelling_a_write_keeps_failure_recovery_for_the_next_success(clock, monkeypatch):
    recovered = []

    async def on_recovered():
        recovered.append(True)

    async def cancelled_write(_frames):
        raise asyncio.CancelledError()

    writer = JournalWriter(
        FakeSessionMaker(), identities=NoIdentities(), on_db_recovered=on_recovered,
    )
    writer._degraded = True
    monkeypatch.setattr(writer, "write_batch", cancelled_write)

    with pytest.raises(asyncio.CancelledError):
        await writer.write_with_retry([_frame("op-1")])

    assert writer._degraded is True
    assert recovered == []

    async def successful_write(_frames):
        return None

    monkeypatch.setattr(writer, "write_batch", successful_write)
    assert await writer.write_with_retry([_frame("op-1")]) is True
    assert writer._degraded is False
    assert recovered == [True]
