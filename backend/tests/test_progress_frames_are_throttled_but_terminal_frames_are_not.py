"""Fifty progress frames for one operation in one second are one write; a Success is written
at once.

An image pull reports progress many times a second and every report is cumulative, so the
writer holds a progress-only frame for an operation it wrote less than a second ago (the newest
replaces the older) and flushes it when the second is up. A frame that changes the status is
never held, and a terminal one clears whatever was held for that operation. Under load a
progress-only frame is the first thing dropped from a full inbox; anything else waits.
"""

import asyncio
import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app.services.task_journal import follower_writer as writer_module  # noqa: E402
from app.services.task_journal.follower_writer import JournalWriter  # noqa: E402
from tests._follower_fakes import NoIdentities  # noqa: E402
from tests._journal_fakes import FakeSessionMaker  # noqa: E402


def _frame(status="Running", code=103, progress=None, op="op-9"):
    return {
        "type": "operation", "host": "nuc1", "operation_id": op, "description": "Creating instance",
        "status": status, "status_code": code, "progress": progress, "err": "",
        "class": "task", "may_cancel": True, "resources": {"instances": ["web-02"]},
        "project": "cust-a", "created_at": None, "updated_at": None, "location": None,
        "requestor": None,
    }


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(writer_module.time, "monotonic", lambda: now[0])
    return now


def _writer(maker=None):
    return JournalWriter(maker or FakeSessionMaker(), identities=NoIdentities())


def test_fifty_progress_frames_in_one_second_coalesce_to_one_write(clock):
    writer = _writer()
    frames = [_frame(progress=f"rootfs: {i}%") for i in range(50)]

    first = writer.coalesce(frames[:25])
    clock[0] += 0.5
    second = writer.coalesce(frames[25:])

    assert [f["progress"] for f in first] == ["rootfs: 0%"], "the first report is written"
    assert second == [], "everything inside the same second is held"
    assert writer._pending_progress[("nuc1", "op-9")]["progress"] == "rootfs: 49%", "the newest is kept"


def test_the_held_progress_flushes_when_its_second_is_up(clock):
    writer = _writer()
    writer.coalesce([_frame(progress="rootfs: 0%"), _frame(progress="rootfs: 5%")])

    clock[0] += 1.0
    flushed = writer.coalesce([])

    assert [f["progress"] for f in flushed] == ["rootfs: 5%"]
    assert writer._pending_progress == {}


def test_a_terminal_frame_is_written_at_once_and_clears_the_held_progress(clock):
    writer = _writer()
    writer.coalesce([_frame(progress="rootfs: 0%"), _frame(progress="rootfs: 5%")])
    clock[0] += 0.1

    out = writer.coalesce([_frame(status="Success", code=200, progress="rootfs: 100%")])

    assert [f["status"] for f in out] == ["Success"]
    assert writer._pending_progress == {}, "a stale progress report must not land after success"
    assert ("nuc1", "op-9") not in writer._last_status, "terminal operations are forgotten"


def test_a_status_change_inside_the_second_is_not_held(clock):
    writer = _writer()
    writer.coalesce([_frame(status="Pending", code=105)])
    clock[0] += 0.1

    out = writer.coalesce([_frame(status="Running", code=103, progress="rootfs: 1%")])

    assert [f["status"] for f in out] == ["Running"]


def test_operations_are_throttled_independently(clock):
    writer = _writer()
    out = writer.coalesce([_frame(progress="a", op="op-1"), _frame(progress="b", op="op-2")])
    assert {f["operation_id"] for f in out} == {"op-1", "op-2"}
    clock[0] += 0.2
    assert writer.coalesce([_frame(progress="c", op="op-1"), _frame(progress="d", op="op-2")]) == []
    assert set(writer._pending_progress) == {("nuc1", "op-1"), ("nuc1", "op-2")}


def test_lifecycle_frames_pass_straight_through(clock):
    writer = _writer()
    frame = {"type": "lifecycle", "host": "nuc1", "action": "instance-started", "name": "web-02"}
    assert writer.coalesce([frame]) == [frame]


async def test_a_full_inbox_drops_progress_first_and_holds_everything_else(monkeypatch):
    monkeypatch.setattr(writer_module, "INBOX_MAX", 2)
    writer = _writer()
    writer._inbox = asyncio.Queue(maxsize=2)
    await writer.submit(_frame(progress="1%"))
    await writer.submit(_frame(progress="2%"))
    assert writer._inbox.full()

    await writer.submit(_frame(progress="3%"))
    assert writer.dropped_progress == 1 and writer._inbox.qsize() == 2

    terminal = asyncio.create_task(writer.submit(_frame(status="Success", code=200)))
    await asyncio.sleep(0)
    assert not terminal.done(), "a status frame waits for a slot rather than being dropped"
    writer._inbox.get_nowait()
    await terminal
    assert writer._inbox.qsize() == 2


async def test_one_batch_is_one_transaction_with_one_upsert_per_frame():
    maker = FakeSessionMaker()
    writer = _writer(maker)

    await writer.write_batch([_frame(progress="1%", op="op-1"), _frame(status="Success", code=200, op="op-2")])

    assert maker.opened == 1 and maker.sessions[0].commits == 1
    upserts = [p for sql, p in maker.sessions[0].calls if "task_journal" in sql and p and "operation_id" in p]
    assert list(dict.fromkeys(p["operation_id"] for p in upserts)) == ["op-1", "op-2"]
    assert writer.written == 2


async def test_a_refused_batch_is_retried_with_backoff_and_a_recovery_reseeds(monkeypatch):
    delays = []

    async def fake_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(writer_module.asyncio, "sleep", fake_sleep)
    reseeds = []

    async def reseed():
        reseeds.append(True)

    maker = FakeSessionMaker(fail_times=3)
    writer = JournalWriter(maker, identities=NoIdentities(), on_db_recovered=reseed)

    assert await writer.write_with_retry([_frame()]) is True

    assert delays == [0.5, 1.0, 2.0], "the batch is held through the backoff, not dropped"
    assert maker.opened == 4
    assert reseeds == [True], "once the database answers again every connected cluster is reseeded"
    assert writer._degraded is False


async def test_a_frame_without_an_operation_id_is_skipped_at_debug(caplog):
    import logging

    maker = FakeSessionMaker()
    writer = _writer(maker)
    with caplog.at_level(logging.DEBUG, logger="app.services.task_journal.follower_frames"):
        await writer.write_batch([_frame(op="")])
    assert not any("task_journal" in sql for sql in maker.statements)
    assert any(r.levelno == logging.DEBUG and "skipped" in r.getMessage() for r in caplog.records)
