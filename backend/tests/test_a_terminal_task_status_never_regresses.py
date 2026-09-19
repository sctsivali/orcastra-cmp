"""A task the cluster finished stays finished, whatever frame arrives next.

The events stream delivers an operation's frames out of order, twice, and from a daemon whose
clock is not this database's. Two followers can overlap during a hand-over. The one statement
that absorbs all of that is the monotonic UPDATE: the cluster's verdict (a terminal status with
a status code of 200 or more, which only a terminal frame writes) is final, `pending` never
overrides `running`, an older non-terminal frame is a no-op, and a no-op bumps no `seq`, so the
tailer never re-sends a row nothing happened to. A verdict the dashboard's audit set (no code,
or an open one) yields to the cluster's terminal frame and never to an open frame, and `lost`
yields to anything the cluster reports: the scratch-database round trip found a Pending frame,
a router success and a cluster Failure leaving the row on success.

These drive `upsert_operation` against a session that records what it compiled and pin the
text of the statement and the parameters it carried, which is what a hermetic test can prove.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.services.task_journal import repository
from app.services.task_journal.repository import UpsertResult, upsert_operation
from tests._journal_fakes import FakeDB, FakeResult, frame

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
TERMINAL_CASE = (
    "CASE WHEN task_journal.status IN ('success', 'failure', 'cancelled') "
    "AND task_journal.status_code >= 200 THEN task_journal.status"
)
AUDIT_VERDICT_YIELDS_ONLY_TO_A_VERDICT = (
    "WHEN task_journal.status IN ('success', 'failure', 'cancelled') "
    "AND CAST(:new_status AS varchar) NOT IN ('success', 'failure', 'cancelled') "
    "THEN task_journal.status"
)
PENDING_NEVER_OVERRIDES = (
    "WHEN CAST(:new_status AS varchar) = 'pending' AND task_journal.status = 'running' "
    "THEN task_journal.status"
)


def _found(status: str, lxd_updated_at=None, rowcount: int = 0, status_code=None):
    """A session whose SELECT ... FOR UPDATE finds one row and whose UPDATE reports rowcount."""

    def respond(sql, params):
        if sql.startswith("SELECT id, status, lxd_updated_at, status_code"):
            return FakeResult(
                rows=[("11111111-1111-1111-1111-111111111111", status, lxd_updated_at, status_code)]
            )
        if sql.startswith("UPDATE task_journal SET status = CASE"):
            return FakeResult(rowcount=rowcount)
        return None

    return FakeDB(respond)


def _update(db: FakeDB):
    updates = [(sql, p) for sql, p in db.calls if sql.startswith("UPDATE task_journal")]
    assert len(updates) == 1, [sql[:60] for sql, _ in db.calls]
    return updates[0]


async def _run(db: FakeDB, fr) -> UpsertResult:
    return await upsert_operation(db, fr)


@pytest.mark.asyncio
async def test_the_update_carries_the_monotonic_case_and_the_change_guard():
    db = _found("running", rowcount=1)
    result = await _run(db, frame("success"))
    sql, params = _update(db)
    assert TERMINAL_CASE in sql
    assert AUDIT_VERDICT_YIELDS_ONLY_TO_A_VERDICT in sql
    assert PENDING_NEVER_OVERRIDES in sql
    assert "seq = nextval('task_journal_seq')" in sql
    assert "task_journal.status IS DISTINCT FROM :effective" in sql
    assert "task_journal.progress IS DISTINCT FROM :progress" in sql
    assert sql.count("FOR UPDATE") == 0, "the select locked the row; the update must not re-lock"
    assert db.statements[0].endswith("FOR UPDATE")
    assert params["effective"] == "success"
    assert result == UpsertResult(id="11111111-1111-1111-1111-111111111111", created=False, changed=True)


@pytest.mark.asyncio
async def test_pending_after_running_is_a_no_op():
    db = _found("running", rowcount=0)
    result = await _run(db, frame("pending"))
    _sql, params = _update(db)
    assert params["effective"] == "running", "the guard compares against the status that would stand"
    assert params["new_status"] == "pending"
    assert result.changed is False
    assert not any(sql.startswith("INSERT") for sql in db.statements), "a known row is never re-inserted"


@pytest.mark.asyncio
async def test_an_older_non_terminal_frame_is_a_no_op_and_a_terminal_frame_always_lands():
    db = _found("running", lxd_updated_at=NOW, rowcount=0)
    await _run(db, frame("running", lxd_updated_at=NOW - timedelta(seconds=30), progress="10%"))
    sql, _params = _update(db)
    assert (
        "CAST(:lxd_updated_at AS timestamptz) >= task_journal.lxd_updated_at "
        "OR CAST(:new_status AS varchar) IN ('success', 'failure', 'cancelled')"
    ) in sql, "an older frame is skipped unless it is the terminal one"
    assert "GREATEST(COALESCE(task_journal.lxd_updated_at" in sql, "the daemon clock only ever moves forward"


@pytest.mark.asyncio
async def test_a_duplicate_frame_bumps_no_seq():
    db = _found("running", rowcount=0)
    result = await _run(db, frame("running"))
    assert result.changed is False
    # The only place seq is bumped is inside the guarded UPDATE; nothing else was executed.
    assert [sql[:6] for sql in db.statements] == ["SELECT", "UPDATE"]
    seq_bumps = [sql for sql in db.statements if "nextval('task_journal_seq')" in sql]
    assert len(seq_bumps) == 1 and "IS DISTINCT FROM :effective" in seq_bumps[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["success", "failure", "cancelled"])
async def test_the_cluster_verdict_is_kept_whatever_comes_next(terminal):
    for incoming in ("cancelled", "running", "pending", "success", "failure"):
        db = _found(terminal, rowcount=0, status_code=200 if terminal == "success" else 400)
        await _run(db, frame(incoming))
        _sql, params = _update(db)
        assert params["effective"] == terminal, (terminal, incoming)


@pytest.mark.asyncio
@pytest.mark.parametrize("audit_code", [None, 105, 103])
async def test_an_audit_verdict_yields_to_the_cluster_verdict_and_not_to_an_open_frame(audit_code):
    for incoming in ("running", "pending"):
        db = _found("success", rowcount=0, status_code=audit_code)
        await _run(db, frame(incoming))
        _sql, params = _update(db)
        assert params["effective"] == "success", incoming
    for incoming in ("failure", "cancelled", "success"):
        db = _found("success", rowcount=1, status_code=audit_code)
        await _run(db, frame(incoming))
        sql, params = _update(db)
        assert params["effective"] == incoming, incoming
        assert "THEN NULLIF(:error, '')" in sql, "the cluster's error text replaces the audit's"


@pytest.mark.asyncio
async def test_lost_yields_to_whatever_the_cluster_reports():
    for incoming in ("running", "success", "failure"):
        db = _found("lost", rowcount=1, status_code=103)
        await _run(db, frame(incoming))
        _sql, params = _update(db)
        assert params["effective"] == incoming, incoming


def test_the_effective_status_rule_is_the_case_in_python_form():
    eff = repository._effective
    assert eff("success", "cancelled", cluster_verdict=True) == "success"
    assert eff("success", "cancelled", cluster_verdict=False) == "cancelled"
    assert eff("failure", "running", cluster_verdict=False) == "failure"
    assert eff("running", "pending", cluster_verdict=False) == "running"
    assert eff("pending", "running", cluster_verdict=False) == "running"
    assert eff("running", "failure", cluster_verdict=False) == "failure"
    assert eff("lost", "success", cluster_verdict=False) == "success"
    assert eff("lost", "running", cluster_verdict=False) == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", ["", "   ", None])
async def test_a_blank_operation_id_is_refused_before_any_sql(blank):
    db = FakeDB()
    with pytest.raises(ValueError):
        await upsert_operation(db, frame("running", operation_id=blank))
    assert db.calls == [], "the partial unique index would take '' as one shared key"


@pytest.mark.asyncio
async def test_a_new_operation_inserts_with_on_conflict_and_the_same_guard():
    def respond(sql, params):
        if sql.startswith("INSERT INTO task_journal"):
            return FakeResult(rows=[("22222222-2222-2222-2222-222222222222", True)])
        return None

    db = FakeDB(respond)
    result = await upsert_operation(db, frame("pending", resource_name=None, action=None))
    inserts = [sql for sql in db.statements if sql.startswith("INSERT")]
    assert len(inserts) == 1
    assert "ON CONFLICT (cluster_id, operation_id) WHERE operation_id IS NOT NULL DO UPDATE SET" in inserts[0]
    assert TERMINAL_CASE in inserts[0]
    assert "RETURNING id, (xmax = 0) AS inserted" in inserts[0]
    assert result == UpsertResult(id="22222222-2222-2222-2222-222222222222", created=True, changed=True)
    # A frame with no resource never tries the by-key attach: there is no key to attach on.
    assert not any("SKIP LOCKED" in sql for sql in db.statements)
