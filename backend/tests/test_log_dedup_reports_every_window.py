"""Repeated log lines are collapsed per window, and every window that ended reports its count.

The filter used to report a repeat count after the first window only (its "flushed" flag was set
on the first rollover and never cleared), so a fault that kept logging looked like it stopped
repeating. It also kept every distinct message forever.
"""

import logging

from app.core import logging_config
from app.core.logging_config import LogDeduplicationFilter


def _record(message: str, **extra) -> logging.LogRecord:
    record = logging.LogRecord("app.test", logging.ERROR, __file__, 1, message, None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_every_window_that_ended_reports_its_count(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(logging_config.time, "time", lambda: clock[0])
    dedup = LogDeduplicationFilter(window_seconds=60)
    counts = []
    for window in range(3):
        records = [_record("Vault read failed") for _ in range(5)]
        passed = [r for r in records if dedup.filter(r)]
        assert len(passed) == 1, f"window {window}: only the first line passes"
        counts.append(getattr(passed[0], "dedup_count", None))
        clock[0] += 61
    assert counts == [None, 5, 5]


def test_a_single_line_per_window_carries_no_count(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(logging_config.time, "time", lambda: clock[0])
    dedup = LogDeduplicationFilter(window_seconds=60)
    for _ in range(3):
        record = _record("once a minute")
        assert dedup.filter(record) is True
        assert not hasattr(record, "dedup_count")
        clock[0] += 61


def test_distinct_messages_are_bounded():
    dedup = LogDeduplicationFilter(window_seconds=60, max_keys=100)
    for i in range(10_000):
        assert dedup.filter(_record(f"unique message {i}")) is True
    assert dedup.tracked() <= 100


def test_access_and_audit_records_always_pass():
    dedup = LogDeduplicationFilter(window_seconds=60)
    for _ in range(3):
        assert dedup.filter(_record("GET /health", access_log_data={"status": 200})) is True
        assert dedup.filter(_record("vault.secret.read", audit_log_data={"event_id": "e"})) is True


def test_shutdown_reports_what_is_still_suppressed(monkeypatch, caplog):
    clock = [1000.0]
    monkeypatch.setattr(logging_config.time, "time", lambda: clock[0])
    dedup = LogDeduplicationFilter(window_seconds=60)
    for _ in range(4):
        dedup.filter(_record("still failing"))
    caplog.set_level(logging.ERROR, logger="app.test")
    dedup.flush_pending()
    assert "[repeated 4x in 60" in caplog.text
    assert dedup.tracked() == 0
