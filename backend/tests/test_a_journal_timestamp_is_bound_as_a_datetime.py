"""asyncpg binds a `CAST(:ts AS timestamptz)` parameter from a datetime only.

Found live: the first boot of the journal dropped every audit row with
`invalid input for query argument $1 ... expected a datetime.date or datetime.datetime
instance, got 'str'`, because the audit record carries `@timestamp` as ISO text and the
hermetic fakes never typed the bind. The log insert now coerces at the boundary, and this
pins the coercion for the two text shapes the writers hand it (an audit record with
microseconds and an offset, an LXD warning with nanoseconds and a Z) plus the passthrough.
"""

from datetime import datetime, timezone

from app.services.task_journal.repository_log import _log_params


def _row(ts):
    return {"ts": ts, "kind": "audit", "severity": "info", "action": "x", "message": "m"}


def test_an_audit_timestamp_becomes_an_aware_datetime():
    bound = _log_params(_row("2026-09-19T05:14:31.638317+00:00"))["ts"]
    assert isinstance(bound, datetime)
    assert bound.tzinfo is not None
    assert bound == datetime(2026, 9, 19, 5, 14, 31, 638317, tzinfo=timezone.utc)


def test_an_lxd_warning_timestamp_with_nanoseconds_becomes_a_datetime():
    bound = _log_params(_row("2026-09-19T03:33:07.594848829Z"))["ts"]
    assert isinstance(bound, datetime)
    assert bound.microsecond == 594848
    assert bound.tzinfo is not None


def test_a_datetime_passes_through_and_garbage_falls_back_to_now():
    now = datetime.now(timezone.utc)
    assert _log_params(_row(now))["ts"] is now
    # None reaches COALESCE(..., now()) in the statement; garbage must not raise.
    assert _log_params(_row(None))["ts"] is None
    assert _log_params(_row("yesterday-ish"))["ts"] is None
    assert _log_params(_row(1758258871))["ts"] is None
