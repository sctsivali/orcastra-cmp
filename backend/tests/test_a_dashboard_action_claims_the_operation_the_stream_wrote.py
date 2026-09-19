"""One click, one row: the audit record and the LXD frame land on the same task.

The follower writes an `lxd_operation` row with no actor the instant LXD announces the
operation; the router's audit record arrives a moment later (or, for a `wait=True` action,
after the frame has already closed the row). Whichever side lands second must find the other's
row and take it, under a lock that a concurrent claimant skips rather than waits on, and by an
operation id when the router knows one. The alternative is two rows per click, and a panel that
shows the operator's own start twice.
"""

import pytest

from app.services.task_journal import repository
from app.services.task_journal.repository import (
    attach_operation_by_id,
    claim_operation_for_audit,
    upsert_operation,
)
from app.services.task_journal.sink import JournalSink
from app.services.task_journal.sink_mapping import MODE_ATTACH_BY_ID, record_to_task
from tests._journal_fakes import FakeDB, FakeResult, actor, audit_record, frame, key

ROW = "33333333-3333-3333-3333-333333333333"


@pytest.mark.asyncio
async def test_the_claim_takes_the_newest_unclaimed_stream_row_under_skip_locked():
    db = FakeDB(lambda sql, p: FakeResult(rows=[(ROW,)]) if sql.startswith("UPDATE") else None)
    claimed = await claim_operation_for_audit(
        db, key=key(), actor=actor(), result="success", audit_error=None, extra={"job_id": "j1"}
    )
    assert claimed == ROW
    sql, params = db.calls[0]
    # The audit is the one writer that knows the resource when LXD reported none ("Creating
    # instance" and "Deleting project" arrive with `resources: {}`): it fills what the row
    # lacks and never overwrites what the daemon said.
    assert sql.startswith(
        "UPDATE task_journal SET resource_type = COALESCE(task_journal.resource_type, :resource_type), "
        "resource_name = COALESCE(task_journal.resource_name, :resource_name), "
        "action = COALESCE(task_journal.action, :action), "
    )
    assert "actor_email = :actor_email" in sql
    subquery = sql[sql.index("WHERE id = (SELECT") :]
    for clause in (
        "actor_email IS NULL",
        "api_key_id IS NULL",
        "source IN ('events', 'cli')",
        "created_at >= now() - interval '15 minutes'",
        "ORDER BY created_at DESC LIMIT 1 FOR UPDATE SKIP LOCKED",
    ):
        assert clause in subquery, clause
    assert "RETURNING id" in sql
    for column in ("actor_role", "actor_kind", "api_key_id", "source", "request_id"):
        assert f"{column} = :{column}" in sql
    assert "metadata = task_journal.metadata || CAST(:extra AS jsonb)" in sql
    assert "ELSE COALESCE(task_journal.error, NULLIF(:audit_error, '')) END" in sql
    assert "CAST(:kind AS varchar) IN ('rebuild', 'upload')" in sql, "a job's audit verdict wins"
    assert "CASE WHEN task_journal.status IN ('success', 'failure', 'cancelled', 'lost')" in sql
    assert params["new_status"] == "success"
    assert params["source"] == "dashboard"
    assert params["extra"] == '{"job_id": "j1"}'


@pytest.mark.asyncio
async def test_an_accepted_record_claims_as_running_and_nothing_found_is_none():
    db = FakeDB()
    assert await claim_operation_for_audit(
        db, key=key(), actor=actor(), result="accepted", audit_error=None, extra={}
    ) is None
    assert db.calls[0][1]["new_status"] == "running"
    # With no key match, the newest unnamed stream row for the same action on the same
    # cluster is tried once, and only a row LXD left unnamed qualifies.
    assert len(db.calls) == 2
    second = db.calls[1][0][db.calls[1][0].index("WHERE id = (SELECT") :]
    for clause in (
        "cluster_id = :cluster_id AND project = :project AND action = :action",
        "AND resource_name IS NULL",
        "operation_id IS NOT NULL",
        "actor_email IS NULL",
        "created_at >= now() - interval '15 minutes'",
        "ORDER BY created_at DESC LIMIT 1 FOR UPDATE SKIP LOCKED",
    ):
        assert clause in second, clause


@pytest.mark.asyncio
async def test_a_key_match_never_reaches_the_unnamed_claim():
    db = FakeDB(lambda sql, p: FakeResult(rows=[(ROW,)]) if sql.startswith("UPDATE") else None)
    await claim_operation_for_audit(
        db, key=key(), actor=actor(), result="success", audit_error=None, extra={}
    )
    assert len(db.calls) == 1


def test_a_record_with_an_operation_id_takes_the_attach_by_id_path():
    task = record_to_task(audit_record("instance.create", details={"operation_id": "op-9"}))
    assert task is not None
    assert task.mode == MODE_ATTACH_BY_ID
    assert task.operation_id == "op-9"


@pytest.mark.asyncio
async def test_attach_by_id_updates_a_row_the_stream_already_wrote():
    def respond(sql, params):
        if sql.startswith("SELECT id FROM task_journal"):
            return FakeResult(rows=[(ROW,)])
        return None

    db = FakeDB(respond)
    task_id = await attach_operation_by_id(
        db, cluster_id="nuc1", operation_id="op-9", kind="dashboard_action", key=key(),
        actor=actor(), result="success", description="Creating instance", audit_error=None,
        extra={},
    )
    assert task_id == ROW
    assert db.statements[0].endswith("FOR UPDATE")
    assert db.statements[1].startswith("UPDATE task_journal SET resource_type = COALESCE(task_journal.resource_type")
    assert "WHERE id = CAST(:id AS uuid)" in db.statements[1]
    assert not any(sql.startswith("INSERT") for sql in db.statements)


@pytest.mark.asyncio
async def test_attach_by_id_opens_the_row_when_the_audit_lands_before_the_frame():
    def respond(sql, params):
        if sql.startswith("INSERT"):
            return FakeResult(rows=[(ROW,)])
        return None

    db = FakeDB(respond)
    task_id = await attach_operation_by_id(
        db, cluster_id="nuc1", operation_id="op-9", kind="dashboard_action", key=key(),
        actor=actor(), result="accepted", description="Creating instance", audit_error=None,
        extra={},
    )
    assert task_id == ROW
    insert = db.statements[1]
    assert "ON CONFLICT (cluster_id, operation_id) WHERE operation_id IS NOT NULL DO UPDATE SET" in insert
    assert db.calls[1][1]["new_status"] == "running"
    assert db.calls[1][1]["operation_id"] == "op-9"


@pytest.mark.asyncio
async def test_attach_by_id_refuses_a_blank_operation_id_before_any_sql():
    db = FakeDB()
    with pytest.raises(ValueError):
        await attach_operation_by_id(
            db, cluster_id="nuc1", operation_id=" ", kind="dashboard_action", key=key(),
            actor=actor(), result="success", description="", audit_error=None, extra={},
        )
    assert db.calls == []


@pytest.mark.asyncio
async def test_the_reverse_attach_prefers_an_open_dashboard_row_and_accepts_a_fresh_terminal_one():
    """A `wait=True` start has its audit row closed before its frame arrives. The frame still
    attaches: the by-key subquery has no status filter, and the kind flips to lxd_operation."""

    def respond(sql, params):
        if "SKIP LOCKED" in sql:
            return FakeResult(rows=[(ROW,)])
        return None

    db = FakeDB(respond)
    result = await upsert_operation(db, frame("success"))
    assert result.id == ROW and result.created is False and result.changed is True
    attach = next(sql for sql in db.statements if "SKIP LOCKED" in sql)
    subquery = attach[attach.index("WHERE id = (SELECT") :]
    # A finished dashboard row is this operation only when a wait=True router closed it moments
    # ago; an open row always; an earlier attempt that failed, never (a review finding).
    assert "(status IN ('pending', 'running') OR created_at >= now() - interval '2 minutes')" in subquery
    assert "ORDER BY (status IN ('pending', 'running')) DESC, created_at DESC" in subquery
    assert "operation_id IS NULL" in subquery
    assert "created_at >= now() - interval '15 minutes'" in subquery
    assert "kind = CASE WHEN task_journal.kind = 'dashboard_action' THEN 'lxd_operation'" in attach
    assert "operation_id = :operation_id" in attach
    assert not any(sql.startswith("INSERT") for sql in db.statements)


@pytest.mark.asyncio
async def test_the_sink_routes_an_operation_id_record_to_attach_by_id(monkeypatch):
    calls = []

    async def fake_attach(db, **kwargs):
        calls.append(("attach", kwargs))
        return ROW

    async def fake_claim(db, **kwargs):
        calls.append(("claim", kwargs))
        return None

    async def fake_org(host, db):
        return (None, None)

    monkeypatch.setattr(repository, "attach_operation_by_id", fake_attach)
    monkeypatch.setattr(repository, "claim_operation_for_audit", fake_claim)
    monkeypatch.setattr("app.core.org_scope.resolve_cluster_org", fake_org)
    sink = JournalSink()
    task = record_to_task(audit_record("instance.create", details={"operation_id": "op-9"}))
    task_id = await sink._apply_task(FakeDB(), task)
    assert task_id == ROW
    assert [name for name, _ in calls] == ["attach"]
    assert calls[0][1]["operation_id"] == "op-9"
    assert calls[0][1]["actor"].actor_email == "ops@example.test"
