"""The journal is a copy; the request that produced the record must never pay for it.

`log_activity` runs inside request handlers, inside worker threads under `run_blocking`, and
inside background tasks. Whatever the sink does (not started, disabled, full, broken, or
missing entirely) the caller gets its event id and the stdout record it always got. These pin
that boundary from both sides: the logger's guarded call, and the sink's own `offer`.
"""

import asyncio
import logging
import threading

import pytest

from app.core.request_context import RequestContext, get_request_context, set_request_context
from app.services import activity_logger as activity_logger_module
from app.services.activity_logger import ActivityLogger
from app.services.task_journal import sink as sink_module
from app.services.task_journal.sink import JournalSink, get_journal_sink
from tests._journal_fakes import FakeSessionMaker, audit_record


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def audit_stdout():
    capture = _Capture()
    audit = logging.getLogger("orcastra.audit")
    audit.addHandler(capture)
    audit.setLevel(logging.INFO)
    try:
        yield capture
    finally:
        audit.removeHandler(capture)


def _close(loop, sink):
    """Retire the writer task before the loop goes, so nothing is destroyed while pending."""
    sink._task.cancel()
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()


def _log(action="instance.start"):
    return ActivityLogger().log_activity(
        action=action, target_type="instance", target_id="web-01", target_host="nuc1",
        target_project="default", result="success",
    )


def test_a_sink_that_raises_costs_the_request_nothing(monkeypatch, audit_stdout):
    def broken():
        raise RuntimeError("journal exploded")

    monkeypatch.setattr(sink_module, "get_journal_sink", broken)
    event_id = _log()
    assert event_id.startswith("evt_")
    assert len(audit_stdout.records) == 1
    record = audit_stdout.records[0].audit_log_data
    assert record["action"] == "instance.start"
    assert record["event_id"] == event_id
    assert "actor_extra" not in record, "the copy handed to the sink is a copy, not the audit record"


def test_a_sink_whose_offer_raises_costs_the_request_nothing(monkeypatch, audit_stdout):
    class _Sink:
        def offer(self, record):
            raise RuntimeError("queue gone")

    monkeypatch.setattr(sink_module, "get_journal_sink", lambda: _Sink())
    assert _log().startswith("evt_")
    assert len(audit_stdout.records) == 1


def test_the_logger_reaches_the_sink_through_the_guarded_call(monkeypatch, audit_stdout):
    offered = []

    class _Sink:
        def offer(self, record):
            offered.append(record)

    monkeypatch.setattr(sink_module, "get_journal_sink", lambda: _Sink())
    ctx = RequestContext(user_id="ops@example.test", user_role="partner", extra={"schedule_id": 7})
    previous = get_request_context()
    set_request_context(ctx)
    try:
        event_id = _log()
    finally:
        set_request_context(previous)
    assert [r["event_id"] for r in offered] == [event_id]
    assert offered[0]["actor_extra"] == {"schedule_id": 7}
    assert offered[0]["actor_extra"] is not ctx.extra, "a copy: the sink may not reach into the context"
    assert offered[0]["target"]["project"] == "default"
    assert offered[0]["actor"]["user_id"] == "ops@example.test"


def test_offer_before_start_is_a_no_op():
    sink = JournalSink()
    sink.offer(audit_record())
    assert sink.dropped == 0


def test_offer_after_the_loop_is_gone_is_a_no_op():
    sink = JournalSink()
    loop = asyncio.new_event_loop()
    sink.start(loop, FakeSessionMaker())
    _close(loop, sink)
    sink.offer(audit_record())
    assert sink.dropped == 0


def test_offer_when_the_journal_is_disabled_is_a_no_op(monkeypatch):
    class _Settings:
        task_journal_enabled = False

    monkeypatch.setattr(sink_module, "get_settings", lambda: _Settings())
    sink = JournalSink()
    loop = asyncio.new_event_loop()
    try:
        sink.start(loop, FakeSessionMaker())
        sink.offer(audit_record())
        assert sink._queue.qsize() == 0
    finally:
        _close(loop, sink)


def test_a_full_queue_counts_drops_and_warns_once_a_minute(monkeypatch, caplog):
    monkeypatch.setattr(sink_module, "QUEUE_MAX", 2)
    clock = {"now": 1000.0}
    monkeypatch.setattr(sink_module.time, "monotonic", lambda: clock["now"])
    sink = JournalSink()
    loop = asyncio.new_event_loop()
    try:
        sink.start(loop, FakeSessionMaker())
        with caplog.at_level(logging.WARNING, logger=sink_module.__name__):
            for _ in range(5):
                sink._enqueue(audit_record())
            assert sink.dropped == 3
            clock["now"] += 30
            sink._enqueue(audit_record())
            assert sink.dropped == 4
            warnings = [r for r in caplog.records if "dropped" in r.getMessage()]
            assert len(warnings) == 1, "one warning per minute, not one per record"
            clock["now"] += 31
            sink._enqueue(audit_record())
            warnings = [r for r in caplog.records if "dropped" in r.getMessage()]
            assert len(warnings) == 2
            assert "5 audit records dropped" in warnings[1].getMessage()
    finally:
        _close(loop, sink)


@pytest.mark.asyncio
async def test_offer_from_a_worker_thread_lands_on_the_loop_and_is_written(monkeypatch):
    async def no_org(host, db):
        return (None, None)

    monkeypatch.setattr("app.core.org_scope.resolve_cluster_org", no_org)
    maker = FakeSessionMaker()
    sink = JournalSink()
    sink.start(asyncio.get_running_loop(), maker)
    try:
        thread = threading.Thread(target=sink.offer, args=(audit_record("instance.start"),))
        thread.start()
        thread.join(timeout=2)
        for _ in range(100):
            if maker.statements:
                break
            await asyncio.sleep(0.02)
        assert any(sql.startswith("INSERT INTO cluster_log") for sql in maker.statements)
        assert maker.sessions[0].commits == 1
        assert sink.written == 1
    finally:
        await sink.stop()


def test_the_singleton_is_one_per_worker():
    assert get_journal_sink() is get_journal_sink()


def test_the_logger_module_has_no_import_time_dependency_on_the_sink():
    """The sink imports the catalogue from the logger; the logger reaches the sink only inside
    the guarded call, so neither module can break the other's import."""
    import inspect

    source = inspect.getsource(activity_logger_module)
    top_level = [line for line in source.splitlines() if line.startswith("from app.services.task_journal")]
    assert top_level == []
