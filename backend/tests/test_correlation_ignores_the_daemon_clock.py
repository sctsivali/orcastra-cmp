"""Every correlation window is measured on this database's clock, never on LXD's.

A cluster's clock can sit minutes from the dashboard's. Measured on `lxd_created_at`, the
fifteen-minute window between a dashboard row and its frame would drop every operation of a skewed
daemon, and the panel would show two rows per click for that cluster only. The daemon's timestamps
are stored for display and ordering only; every `interval` and `make_interval` predicate reads
`created_at` or `updated_at`, which Postgres stamped.
"""

import re
from datetime import datetime, timedelta, timezone

import pytest

from app.services.task_journal.repository import (
    attach_requestor,
    claim_operation_for_audit,
    expire_stale_actions,
    mark_lost,
    merge_racing_pairs,
    upsert_operation,
)
from tests._journal_fakes import FakeDB, FakeResult, actor, frame, key

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
SKEW = timedelta(minutes=9)


def _daemon_columns(sql: str):
    return re.findall(r"lxd_(?:created|updated)_at", sql)


def _window_predicates(sql: str):
    """Every `<column> >= now() - ...` or `<column> < now() - ...` comparison in the text."""
    return re.findall(r"(\w+)\s*(?:>=|<)\s*now\(\)\s*-", sql)


@pytest.mark.asyncio
@pytest.mark.parametrize("skew", [SKEW, -SKEW])
async def test_a_frame_from_a_skewed_daemon_still_attaches_to_its_dashboard_row(skew):
    def respond(sql, params):
        if "SKIP LOCKED" in sql:
            return FakeResult(rows=[("44444444-4444-4444-4444-444444444444",)])
        return None

    db = FakeDB(respond)
    result = await upsert_operation(
        db, frame("running", lxd_created_at=NOW + skew, lxd_updated_at=NOW + skew)
    )
    assert result.id == "44444444-4444-4444-4444-444444444444"
    attach = next(sql for sql in db.statements if "SKIP LOCKED" in sql)
    subquery = attach[attach.index("WHERE id = (SELECT") :]
    assert _daemon_columns(subquery) == [], "the window is on Postgres's created_at, not the daemon's clock"
    # Two windows, both on created_at: the 15 minute key window and the 2 minute one a
    # finished dashboard row must fall inside; neither reads the daemon's clock.
    assert _window_predicates(subquery) == ["created_at", "created_at"]
    params = next(p for sql, p in db.calls if "SKIP LOCKED" in sql)
    assert params["lxd_created_at"] == NOW + skew, "the daemon's time is still stored, for display"


@pytest.mark.asyncio
async def test_the_audit_side_claim_is_windowed_on_created_at():
    db = FakeDB()
    await claim_operation_for_audit(
        db, key=key(), actor=actor(), result="success", audit_error=None, extra={}
    )
    sql = db.statements[0]
    subquery = sql[sql.index("WHERE id = (SELECT") :]
    assert _daemon_columns(subquery) == []
    assert _window_predicates(subquery) == ["created_at"]


@pytest.mark.asyncio
async def test_the_requestor_window_is_on_created_at():
    db = FakeDB()
    await attach_requestor(
        db, key=key(), requestor={"protocol": "unix", "username": "root"}, actor_kind="external",
        source="cli", actor_email="root", window_s=120,
    )
    sql = db.statements[0]
    subquery = sql[sql.index("WHERE id = (SELECT") :]
    assert _daemon_columns(subquery) == []
    assert _window_predicates(subquery) == ["created_at"]
    assert db.calls[0][1]["window_s"] == 120.0


@pytest.mark.asyncio
async def test_the_sweeps_age_rows_on_postgres_timestamps():
    db = FakeDB()
    await merge_racing_pairs(db, window_s=900)
    await mark_lost(db, cluster_id="nuc1", seen_ids=["op-1"], older_than_s=60)
    await expire_stale_actions(db, older_than_s=1800)
    for sql in db.statements:
        predicates = _window_predicates(sql)
        assert predicates, sql[:80]
        assert set(predicates) <= {"created_at", "updated_at"}, sql[:80]
        assert _daemon_columns(sql) == []
