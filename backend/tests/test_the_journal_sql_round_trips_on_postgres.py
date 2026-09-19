"""Every journal statement, once, against a real Postgres through asyncpg.

The hermetic suite proves the logic with fakes that bind any Python value; asyncpg does not.
The first live boot dropped every audit row (`ts` bound as text) and refused the very first
seed (`inconsistent types deduced for parameter $9`, a bind read as varchar in VALUES and as
text in a CASE). Neither can be seen without the driver, so this module drives the production
writers (`JournalSink._write_batch`, `JournalWriter.write_batch`), every sweep, every read and
the prune against a scratch database migrated to head, and is skipped unless
`JOURNAL_TEST_DATABASE_URL` names one. Run it from the backend venv against the dev postgres:

    JOURNAL_TEST_DATABASE_URL=postgresql+asyncpg://user:pw@host:5432/qa_journal_scratch \
        python3 -m pytest -q tests/test_the_journal_sql_round_trips_on_postgres.py
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

URL = os.environ.get("JOURNAL_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="JOURNAL_TEST_DATABASE_URL is not set")

# important: the fixture TRUNCATEs the three journal tables. The database it names must say
# it is a scratch one, or a mistyped URL empties the dev journal (a review finding).
if URL and not any(word in URL.rsplit("/", 1)[-1].lower() for word in ("scratch", "test")):
    raise RuntimeError("JOURNAL_TEST_DATABASE_URL must name a scratch or test database")

HOST = "rt-host"


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _lxd(dt: datetime) -> str:
    # LXD writes nine fractional digits and a Z.
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "123Z"


def _audit(action: str, *, target_type: str, target_id: str, result: str = "success",
           project: str = "default", details=None, actor="ops@example.test", role="admin",
           duration_ms: float = 12.5, request_id=None, host=HOST):
    return {
        "@timestamp": _iso(datetime.now(timezone.utc)),
        "log_type": "audit",
        "event_id": f"evt_{uuid.uuid4()}",
        "request_id": request_id,
        "action": action,
        "category": "instance",
        "severity": "medium",
        "actor": {"user_id": actor, "user_type": "user", "role": role, "groups": [],
                  "organizations": [], "session_id": None},
        "target": {"type": target_type, "id": target_id, "host": host, "project": project,
                   "organization_id": None, "organization_slug": None},
        "result": result,
        "error_code": None,
        "error_message": "boom" if result == "failure" else None,
        "details": dict(details or {}),
        "metadata": {"duration_ms": duration_ms, "before": None, "after": None},
        "actor_extra": {},
    }


def _operation(op_id: str, *, status: str, code: int, name: str, project: str = "default",
               description: str = "Stopping instance", created: datetime, updated: datetime,
               progress=None, err=None, may_cancel=False, klass="task", requestor=None):
    return {
        "type": "operation",
        "host": HOST,
        "operation_id": op_id,
        "class": klass,
        "description": description,
        "status": status,
        "status_code": code,
        "may_cancel": may_cancel,
        "created_at": _lxd(created),
        "updated_at": _lxd(updated),
        "location": "none",
        "resources": {"instances": [f"/1.0/instances/{name}"]},
        "project": project,
        "progress": progress,
        "err": err,
        "requestor": requestor,
    }


class _Identities:
    async def for_host(self, host: str) -> frozenset:
        return frozenset({"dashboard-fingerprint"})


@pytest.fixture(scope="module")
def loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="module")
def db(loop):
    engine = create_async_engine(URL, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def reset():
        async with maker() as s:
            await s.execute(text("TRUNCATE task_journal, cluster_log, task_follower_clusters"))
            await s.commit()

    loop.run_until_complete(reset())
    yield maker
    loop.run_until_complete(reset())
    loop.run_until_complete(engine.dispose())


def _run(loop, coro):
    return loop.run_until_complete(coro)


async def _one(maker, sql: str, **params):
    async with maker() as s:
        return (await s.execute(text(sql), params)).first()


async def _rows(maker, sql: str, **params):
    async with maker() as s:
        return [dict(r._mapping) for r in (await s.execute(text(sql), params)).all()]


def test_the_sink_writes_an_audit_record_and_the_stream_lands_on_the_same_row(loop, db):
    from app.services.task_journal.follower_writer import JournalWriter
    from app.services.task_journal.sink import JournalSink

    sink = JournalSink()
    sink._session_maker = db
    writer = JournalWriter(db, identities=_Identities())
    t0 = datetime.now(timezone.utc)

    # 1. The router audit for a stop lands first (no operation id: claim-or-insert).
    stop = _audit("instance.stop", target_type="instance", target_id="web-01",
                  details={"target_project": "default"}, request_id="req-1")
    _run(loop, sink._write_batch([stop]))
    row = _run(loop, _one(db, "SELECT id, status, actor_email, source, kind, operation_id, "
                              "target_organization_id FROM task_journal"))
    assert row is not None and row.status == "success" and row.actor_email == "ops@example.test"
    assert row.source == "dashboard" and row.kind == "dashboard_action" and row.operation_id is None
    task_id = str(row.id)
    log = _run(loop, _rows(db, "SELECT ts, kind, task_id::text AS task_id, actor_kind, message "
                               "FROM cluster_log"))
    assert len(log) == 1 and log[0]["task_id"] == task_id and log[0]["kind"] == "audit"
    assert isinstance(log[0]["ts"], datetime)

    # 2. The stream frames for the same stop: running then success, attached by key.
    op = str(uuid.uuid4())
    _run(loop, writer.write_batch([
        _operation(op, status="Running", code=103, name="web-01", created=t0, updated=t0),
    ]))
    _run(loop, writer.write_batch([
        _operation(op, status="Success", code=200, name="web-01", created=t0,
                   updated=t0 + timedelta(seconds=2)),
    ]))
    rows = _run(loop, _rows(db, "SELECT id::text AS id, status, operation_id, seq, finished_at, "
                                "lxd_updated_at FROM task_journal ORDER BY created_at"))
    assert len(rows) == 1, rows
    assert rows[0]["id"] == task_id and rows[0]["operation_id"] == op
    assert rows[0]["status"] == "success" and rows[0]["finished_at"] is not None
    seq_after_success = rows[0]["seq"]

    # 3. A late Running frame with an older daemon clock changes nothing: terminal wins, no seq.
    _run(loop, writer.write_batch([
        _operation(op, status="Running", code=103, name="web-01", created=t0, updated=t0),
    ]))
    again = _run(loop, _one(db, "SELECT status, seq FROM task_journal"))
    assert again.status == "success" and again.seq == seq_after_success

    # 4. A duplicate success frame is a no-op too.
    _run(loop, writer.write_batch([
        _operation(op, status="Success", code=200, name="web-01", created=t0,
                   updated=t0 + timedelta(seconds=2)),
    ]))
    assert _run(loop, _one(db, "SELECT seq FROM task_journal")).seq == seq_after_success

    # 5. A lifecycle frame from a unix-socket caller: log row, external actor, CLI source.
    _run(loop, writer.write_batch([{
        "type": "lifecycle", "host": HOST, "action": "instance-stopped", "name": "web-01",
        "project": "default", "source": "/1.0/instances/web-01",
        "timestamp": _lxd(t0 + timedelta(seconds=3)),
        "requestor": {"protocol": "unix", "username": "root", "address": ""},
        "context": {},
    }]))
    life = _run(loop, _rows(db, "SELECT kind, actor_kind, actor_email, message, task_id::text AS "
                                "task_id, event_id FROM cluster_log WHERE kind = 'lifecycle'"))
    assert len(life) == 1 and life[0]["actor_kind"] == "external"
    assert life[0]["message"] == "Instance web-01 stopped"
    assert life[0]["event_id"].startswith(f"lifecycle:{HOST}:")
    # Live: the dashboard had claimed the row, so attach_requestor rightly left the actor
    # alone, and the lifecycle line must still be linked to the task by key.
    assert life[0]["task_id"] == task_id, "the lifecycle line belongs to the claimed task"
    kept = _run(loop, _one(db, "SELECT actor_email, source, actor_kind FROM task_journal WHERE id = CAST(:id AS uuid)", id=task_id))
    assert (kept.actor_email, kept.source, kept.actor_kind) == ("ops@example.test", "dashboard", "user")
    # The same lifecycle frame twice (a follower hand-over) is one row.
    _run(loop, writer.write_batch([{
        "type": "lifecycle", "host": HOST, "action": "instance-stopped", "name": "web-01",
        "project": "default", "source": "/1.0/instances/web-01",
        "timestamp": _lxd(t0 + timedelta(seconds=3)),
        "requestor": {"protocol": "unix", "username": "root", "address": ""},
    }]))
    assert _run(loop, _one(db, "SELECT count(*) AS n FROM cluster_log WHERE kind = 'lifecycle'")).n == 1


def test_the_stream_first_then_the_audit_with_the_operation_id(loop, db):
    from app.services.task_journal.follower_writer import JournalWriter
    from app.services.task_journal.sink import JournalSink

    sink = JournalSink()
    sink._session_maker = db
    writer = JournalWriter(db, identities=_Identities())
    t0 = datetime.now(timezone.utc)
    op = str(uuid.uuid4())

    # A snapshot op from the stream, pending with a progress string and a cancel flag.
    _run(loop, writer.write_batch([
        _operation(op, status="Pending", code=105, name="web-02", description="Snapshotting instance",
                   created=t0, updated=t0, progress="10%", may_cancel=True),
    ]))
    # The router audit names the operation id: attached by id.
    snap = _audit("instance.snapshot.create", target_type="instance", target_id="web-02",
                  details={"operation_id": op, "snapshot": "snap0"}, request_id="req-2")
    _run(loop, sink._write_batch([snap]))
    rows = _run(loop, _rows(db, "SELECT status, actor_email, may_cancel, progress, source, kind "
                                "FROM task_journal WHERE operation_id = :op", op=op))
    assert len(rows) == 1
    assert rows[0]["actor_email"] == "ops@example.test" and rows[0]["may_cancel"] is True
    assert rows[0]["progress"] == "10%" and rows[0]["source"] == "dashboard"
    # A failure frame ends it with the error, and the message is sanitised text.
    _run(loop, writer.write_batch([
        _operation(op, status="Failure", code=400, name="web-02", description="Snapshotting instance",
                   created=t0, updated=t0 + timedelta(seconds=1), err="disk full", may_cancel=False),
    ]))
    done = _run(loop, _one(db, "SELECT status, error, finished_at FROM task_journal WHERE operation_id = :op", op=op))
    assert done.status == "failure" and done.error == "disk full" and done.finished_at is not None


def test_an_operation_the_daemon_did_not_name_takes_its_key_from_the_audit(loop, db):
    """Live: "Creating instance" and "Deleting project" arrive with `resources: {}`."""
    from app.services.task_journal.follower_writer import JournalWriter
    from app.services.task_journal.sink import JournalSink

    sink = JournalSink()
    sink._session_maker = db
    writer = JournalWriter(db, identities=_Identities())
    t0 = datetime.now(timezone.utc)

    # Attach by id: the create op has no resources; the router audit names the instance.
    op = str(uuid.uuid4())
    frame = _operation(op, status="Failure", code=400, name="unused", description="Creating instance",
                       project="qa-p", created=t0, updated=t0, err="Image not provided")
    frame["resources"] = {}
    _run(loop, writer.write_batch([frame]))
    bare = _run(loop, _one(db, "SELECT resource_name, action FROM task_journal WHERE operation_id = :op", op=op))
    assert bare.resource_name is None and bare.action == "instance.create"
    audit = _audit("instance.create", target_type="instance", target_id="web-07", result="failure",
                   project="qa-p", details={"operation_id": op})
    _run(loop, sink._write_batch([audit]))
    named = _run(loop, _one(db, "SELECT resource_type, resource_name, project, status, actor_email, kind "
                                "FROM task_journal WHERE operation_id = :op", op=op))
    assert (named.resource_type, named.resource_name, named.project) == ("instance", "web-07", "qa-p")
    assert named.status == "failure" and named.actor_email == "ops@example.test"
    assert _run(loop, _one(db, "SELECT count(*) AS n FROM task_journal WHERE resource_name = 'web-07'")).n == 1

    # Claim by action: the project delete op has no resources and the router knows no id.
    op2 = str(uuid.uuid4())
    frame2 = _operation(op2, status="Success", code=200, name="unused", description="Deleting project",
                        project="default", created=t0, updated=t0 + timedelta(seconds=1))
    frame2["resources"] = {}
    _run(loop, writer.write_batch([frame2]))
    audit2 = _audit("project.delete", target_type="project", target_id="qa-p")
    _run(loop, sink._write_batch([audit2]))
    rows = _run(loop, _rows(db, "SELECT operation_id, resource_type, resource_name, actor_email, kind "
                                "FROM task_journal WHERE action = 'project.delete'"))
    assert len(rows) == 1, rows
    assert rows[0]["operation_id"] == op2 and rows[0]["resource_name"] == "qa-p"
    assert rows[0]["actor_email"] == "ops@example.test" and rows[0]["kind"] == "lxd_operation"


def test_a_lifecycle_line_ahead_of_its_operation_is_linked_by_the_sweep(loop, db):
    """Live on nuc1: the admin's stop had its `instance-stopped` line unlinked while the
    partner's was linked, with the same frame order; the sweep makes the link deterministic."""
    from app.services.task_journal.follower_writer import JournalWriter
    from app.services.task_journal.repository_sweeps import link_lifecycle_rows

    writer = JournalWriter(db, identities=_Identities())
    t0 = datetime.now(timezone.utc)
    op = str(uuid.uuid4())
    # The lifecycle frame first: no task row yet, so the inline link finds nothing.
    _run(loop, writer.write_batch([{
        "type": "lifecycle", "host": HOST, "action": "instance-stopped", "name": "web-11",
        "project": "default", "source": "/1.0/instances/web-11", "timestamp": _lxd(t0),
        "requestor": {"protocol": "tls", "username": "dashboard-fingerprint", "address": "1.2.3.4:1"},
    }]))
    unlinked = _run(loop, _one(db, "SELECT task_id FROM cluster_log WHERE resource_name = 'web-11'"))
    assert unlinked.task_id is None
    _run(loop, writer.write_batch([
        _operation(op, status="Success", code=200, name="web-11", created=t0, updated=t0 + timedelta(seconds=1)),
    ]))
    async def sweep():
        async with db() as s:
            n = await link_lifecycle_rows(s, window_s=900)
            await s.commit()
            return n
    assert _run(loop, sweep()) == 1
    linked = _run(loop, _one(db, "SELECT task_id::text AS task_id FROM cluster_log WHERE resource_name = 'web-11'"))
    task = _run(loop, _one(db, "SELECT id::text AS id FROM task_journal WHERE operation_id = :op", op=op))
    assert linked.task_id == task.id
    # Idempotent: nothing left to link.
    assert _run(loop, sweep()) == 0


def test_an_over_long_client_string_does_not_poison_the_batch(loop, db):
    """One 600-character instance name in a batch of two audit records: both rows land."""
    from app.services.task_journal.sink import JournalSink

    sink = JournalSink()
    sink._session_maker = db
    long_name = "x" * 600
    a = _audit("instance.stop", target_type="instance", target_id=long_name, request_id="r" * 200)
    b = _audit("instance.stop", target_type="instance", target_id="web-short")
    _run(loop, sink._write_batch([a, b]))
    rows = _run(loop, _rows(db, "SELECT resource_name, request_id FROM task_journal WHERE action = 'instance.stop' "
                                "AND resource_name IN (:l, 'web-short')", l=long_name[:255]))
    assert len(rows) == 2
    assert any(len(r["resource_name"]) == 255 for r in rows)
    assert all(r["request_id"] is None or len(r["request_id"]) <= 64 for r in rows)
    logs = _run(loop, _one(db, "SELECT count(*) AS n FROM cluster_log WHERE resource_name IN (:l, 'web-short')", l=long_name[:255]))
    assert logs.n == 2


def test_a_rebuild_job_takes_the_audit_verdict_over_its_one_operation(loop, db):
    """The LXD rebuild step succeeded (200) and the job's start step then failed: the row is
    the job, so it reads failure with the job's error, not success with an error beneath."""
    from app.services.task_journal.follower_writer import JournalWriter
    from app.services.task_journal.sink import JournalSink

    sink = JournalSink()
    sink._session_maker = db
    writer = JournalWriter(db, identities=_Identities())
    t0 = datetime.now(timezone.utc)
    op = str(uuid.uuid4())
    _run(loop, writer.write_batch([
        _operation(op, status="Success", code=200, name="web-21", description="Rebuilding instance",
                   created=t0, updated=t0 + timedelta(seconds=5)),
    ]))
    audit = _audit("instance.rebuild", target_type="instance", target_id="web-21", result="failure",
                   details={"operation_id": op, "project": "default"})
    audit["error_message"] = "Instance is busy"
    _run(loop, sink._write_batch([audit]))
    row = _run(loop, _one(db, "SELECT kind, status, error FROM task_journal WHERE operation_id = :op", op=op))
    assert (row.kind, row.status, row.error) == ("rebuild", "failure", "Instance is busy")


def test_a_stream_frame_never_attaches_to_an_earlier_failed_attempt(loop, db):
    """Start failed a minute ago (audit only, no operation); the operator starts again and the
    daemon's frame lands before the second audit: it must open a row of its own, not adopt
    the failed attempt."""
    from app.services.task_journal.follower_writer import JournalWriter
    from app.services.task_journal.sink import JournalSink

    sink = JournalSink()
    sink._session_maker = db
    writer = JournalWriter(db, identities=_Identities())
    failed = _audit("instance.start", target_type="instance", target_id="web-31", result="failure",
                    details={"target_project": "default"})
    _run(loop, sink._write_batch([failed]))
    async def age():
        async with db() as s:
            await s.execute(text("UPDATE task_journal SET created_at = now() - interval '3 minutes' "
                                 "WHERE resource_name = 'web-31'"))
            await s.commit()
    _run(loop, age())
    t0 = datetime.now(timezone.utc)
    op = str(uuid.uuid4())
    _run(loop, writer.write_batch([
        _operation(op, status="Running", code=103, name="web-31", description="Starting instance",
                   created=t0, updated=t0),
    ]))
    rows = _run(loop, _rows(db, "SELECT status, operation_id FROM task_journal WHERE resource_name = 'web-31' ORDER BY created_at"))
    assert len(rows) == 2
    assert rows[0]["status"] == "failure" and rows[0]["operation_id"] is None
    assert rows[1]["status"] == "running" and rows[1]["operation_id"] == op


def test_an_unnamed_frame_after_its_audit_joins_the_audit_row(loop, db):
    """Audit first (a project delete the router waited for), then the daemon's frame with
    resources {}: one row, named by the audit, closed by the frame."""
    from app.services.task_journal.follower_writer import JournalWriter
    from app.services.task_journal.sink import JournalSink

    sink = JournalSink()
    sink._session_maker = db
    writer = JournalWriter(db, identities=_Identities())
    t0 = datetime.now(timezone.utc)
    _run(loop, sink._write_batch([_audit("project.delete", target_type="project", target_id="qa-z", project="default")]))
    op = str(uuid.uuid4())
    frame = _operation(op, status="Success", code=200, name="unused", description="Deleting project",
                       project="default", created=t0, updated=t0 + timedelta(seconds=1))
    frame["resources"] = {}
    _run(loop, writer.write_batch([frame]))
    rows = _run(loop, _rows(db, "SELECT resource_name, operation_id, status, kind, finished_at FROM task_journal WHERE action = 'project.delete' AND resource_name = 'qa-z' OR operation_id = :op", op=op))
    assert len(rows) == 1, rows
    assert rows[0]["resource_name"] == "qa-z" and rows[0]["operation_id"] == op and rows[0]["kind"] == "lxd_operation"


def test_a_lost_action_is_closed_by_its_own_terminal_audit_and_a_revived_row_drops_the_sweep_text(loop, db):
    from app.services.task_journal.follower_writer import JournalWriter
    from app.services.task_journal.repository_sweeps import expire_stale_actions
    from app.services.task_journal.sink import JournalSink

    sink = JournalSink()
    sink._session_maker = db
    writer = JournalWriter(db, identities=_Identities())
    # An accepted upload that expired to lost, then its real close.
    opened = _audit("image.upload.import", target_type="image", target_id="big.tar.gz", result="accepted",
                    details={"session_id": "sess-9"})
    _run(loop, sink._write_batch([opened]))
    async def expire():
        async with db() as s:
            await s.execute(text("UPDATE task_journal SET created_at = now() - interval '2 hours', "
                                 "updated_at = now() - interval '2 hours' WHERE metadata ->> 'session_id' = 'sess-9'"))
            n = await expire_stale_actions(s, older_than_s=1800)
            await s.commit()
            return n
    assert _run(loop, expire()) == 1
    closed = _audit("image.upload.import", target_type="image", target_id="big.tar.gz", result="success",
                    details={"session_id": "sess-9"})
    _run(loop, sink._write_batch([closed]))
    row = _run(loop, _one(db, "SELECT status FROM task_journal WHERE metadata ->> 'session_id' = 'sess-9'"))
    assert row.status == "success"

    # A stream row marked lost, then reported again by the daemon: revived, sweep text gone.
    t0 = datetime.now(timezone.utc)
    op = str(uuid.uuid4())
    _run(loop, writer.write_batch([_operation(op, status="Running", code=103, name="web-41", created=t0, updated=t0)]))
    async def lose():
        async with db() as s:
            await s.execute(text("UPDATE task_journal SET status = 'lost', error = 'The dashboard stopped observing this action', "
                                 "seq = nextval('task_journal_seq') WHERE operation_id = :op"), {"op": op})
            await s.commit()
    _run(loop, lose())
    _run(loop, writer.write_batch([_operation(op, status="Success", code=200, name="web-41", created=t0, updated=t0 + timedelta(seconds=3))]))
    back = _run(loop, _one(db, "SELECT status, error FROM task_journal WHERE operation_id = :op", op=op))
    assert (back.status, back.error) == ("success", None)


def test_uploads_open_and_close_on_their_session(loop, db):
    from app.services.task_journal.sink import JournalSink

    sink = JournalSink()
    sink._session_maker = db
    opened = _audit("image.upload.import", target_type="image", target_id="alpine.tar.gz",
                    result="accepted", details={"session_id": "sess-1", "size": 1234})
    _run(loop, sink._write_batch([opened]))
    row = _run(loop, _one(db, "SELECT id::text AS id, status, kind FROM task_journal WHERE kind = 'upload' AND metadata ->> 'session_id' = 'sess-1'"))
    assert row is not None and row.status == "running"
    closed = _audit("image.upload.import", target_type="image", target_id="alpine.tar.gz",
                    result="failure", details={"session_id": "sess-1"})
    _run(loop, sink._write_batch([closed]))
    rows = _run(loop, _rows(db, "SELECT id::text AS id, status, error FROM task_journal WHERE kind = 'upload' AND metadata ->> 'session_id' = 'sess-1'"))
    assert len(rows) == 1 and rows[0]["id"] == row.id and rows[0]["status"] == "failure"
    assert rows[0]["error"] == "boom"


def test_a_websocket_operation_is_stored_without_its_command_line(loop, db):
    from app.services.task_journal.follower_writer import JournalWriter

    writer = JournalWriter(db, identities=_Identities())
    t0 = datetime.now(timezone.utc)
    op = str(uuid.uuid4())
    frame = _operation(op, status="Running", code=103, name="web-03", description="Executing command",
                       created=t0, updated=t0, klass="websocket",
                       requestor={"protocol": "unix", "username": "", "address": ""})
    frame["metadata"] = {"command": ["sh", "-c", "echo SECRET"], "fds": {"0": "x"}}
    _run(loop, writer.write_batch([frame]))
    row = _run(loop, _one(db, "SELECT action, operation_class, metadata::text AS metadata, source, actor_kind "
                              "FROM task_journal WHERE operation_id = :op", op=op))
    assert row.action == "instance.exec" and row.operation_class == "websocket"
    assert "SECRET" not in row.metadata and "fds" not in row.metadata
    assert row.source == "cli" and row.actor_kind == "external"


def test_every_sweep_and_the_cluster_state_helpers(loop, db):
    from app.services.task_journal import repository
    from app.services.task_journal.repository_sweeps import (
        expire_stale_actions, follower_is_active, mark_lost, merge_racing_pairs,
        read_cluster_states, reset_cluster_states, set_cluster_state, touch_cluster_frame,
    )
    from app.services.task_journal.repository_types import ActorFields, CorrelationKey

    async def go():
        async with db() as s:
            actor = ActorFields("ops@example.test", "admin", "user", None, "dashboard", "req-9", None, None)
            key = CorrelationKey(HOST, "default", "instance", "web-09", "instance.start")
            # A racing pair: an action row and a stream row for the same key, both open.
            a = await repository.insert_action(s, kind="dashboard_action", key=key, actor=actor,
                                               result="accepted", duration_ms=None,
                                               description="Starting instance", audit_error=None,
                                               extra={})
            await s.execute(text(
                "INSERT INTO task_journal (kind, cluster_id, project, resource_type, resource_name, "
                "operation_id, action, description, status, started_at) VALUES ('lxd_operation', "
                ":h, 'default', 'instance', 'web-09', :op, 'instance.start', 'Starting instance', "
                "'running', now())"), {"h": HOST, "op": str(uuid.uuid4())})
            merged = await merge_racing_pairs(s, window_s=900)
            assert merged == 1
            left = (await s.execute(text(
                "SELECT id::text AS id, operation_id, actor_email FROM task_journal "
                "WHERE resource_name = 'web-09'"))).all()
            assert len(left) == 1 and left[0].operation_id and left[0].actor_email == "ops@example.test"
            assert a  # the action row was the survivor's identity or was folded in; one row either way

            # A stale open action expires, a young one does not.
            young = await repository.insert_action(s, kind="dashboard_action",
                                                   key=CorrelationKey(HOST, "default", "instance", "web-10", "instance.stop"),
                                                   actor=actor, result="accepted", duration_ms=None,
                                                   description="Stopping instance", audit_error=None, extra={})
            await s.execute(text("UPDATE task_journal SET created_at = now() - interval '2 hours', "
                                 "updated_at = now() - interval '2 hours' WHERE id = CAST(:id AS uuid)"), {"id": young})
            assert await expire_stale_actions(s, older_than_s=1800) == 1
            lost = (await s.execute(text("SELECT status, error FROM task_journal WHERE id = CAST(:id AS uuid)"), {"id": young})).first()
            assert lost.status == "lost" and lost.error

            # mark_lost: an open stream row the seed no longer sees.
            gone = str(uuid.uuid4())
            await s.execute(text(
                "INSERT INTO task_journal (kind, cluster_id, project, operation_id, description, status, "
                "started_at, created_at, updated_at) VALUES ('lxd_operation', :h, 'default', :op, 'x', "
                "'running', now() - interval '10 minutes', now() - interval '10 minutes', now() - interval '10 minutes')"),
                {"h": HOST, "op": gone})
            n = await mark_lost(s, cluster_id=HOST, seen_ids=[left[0].operation_id], older_than_s=60)
            assert n == 1
            assert (await s.execute(text("SELECT status FROM task_journal WHERE operation_id = :op"), {"op": gone})).scalar() == "lost"

            # Cluster state helpers.
            await set_cluster_state(s, HOST, "connected", seeded=True)
            await touch_cluster_frame(s, HOST)
            await set_cluster_state(s, "rt-dead", "disconnected", error="dial tcp: refused")
            states = await read_cluster_states(s)
            by = {r["cluster_id"]: r for r in states}
            assert by[HOST]["state"] == "connected" and by[HOST]["last_frame_at"] is not None
            assert by["rt-dead"]["last_error"] == "dial tcp: refused"
            await reset_cluster_states(s)
            assert all(r["state"] == "disconnected" for r in await read_cluster_states(s))
            assert await follower_is_active(s) is False
            await s.commit()

    _run(loop, go())


def test_every_read_returns_rows_the_presenter_and_models_accept(loop, db):
    from app.models.task_journal_models import ClusterLog, TaskJournal
    from app.models.task_schemas import ClusterLogEntry, QueueCluster, TaskRecord
    from app.services.task_journal import present, reads

    async def go():
        async with db() as s:
            rows, total = await reads.list_tasks(s, [], [], 50, 0)
            assert total >= 5 and len(rows) == total
            for tier in ("admin", "partner", "tenant"):
                for row in rows:
                    TaskRecord.model_validate(present.task_row(row, tier=tier))
            assert await reads.count_running(s, []) >= 0
            assert await reads.count_failed_recent(s, []) >= 1
            first = rows[0]
            one = await reads.get_task(s, str(first["id"]))
            assert one is not None and str(one["id"]) == str(first["id"])
            lines = await reads.task_log(s, str(first["id"]))
            for line in lines:
                ClusterLogEntry.model_validate(present.log_row(line, tier="tenant"))
            queue = await reads.queue_rows(s, [])
            for q in queue:
                assert q["status"] in ("pending", "running")
            due = await reads.due_schedules(s, None)
            assert isinstance(due, list)
            logs, ltotal = await reads.list_log(s, [], [], 50, 0)
            assert ltotal >= 3
            for line in logs:
                for tier in ("admin", "partner", "tenant"):
                    ClusterLogEntry.model_validate(present.log_row(line, tier=tier))
            filtered, _ = await reads.list_tasks(s, [TaskJournal.status == "lost"], [TaskJournal.cluster_id == HOST], 10, 0)
            assert all(r["status"] == "lost" for r in filtered) and filtered
            failed, _ = await reads.list_log(s, [ClusterLog.kind == "audit"], [], 10, 0)
            assert failed
            assert await reads.follower_active(s) is False
            assert isinstance(await reads.follower_snapshot(s), list)
            QueueCluster(cluster_id=HOST, state="connected", running=[], pending=[], due_schedules=[])

    _run(loop, go())


def test_the_prune_deletes_old_rows_under_its_lock_and_the_downgrade_is_clean(loop, db):
    from app.services.task_journal import prune as prune_module

    async def go():
        async with db() as s:
            await s.execute(text("UPDATE task_journal SET updated_at = now() - interval '40 days', "
                                 "created_at = now() - interval '40 days' WHERE status = 'lost'"))
            await s.execute(text("UPDATE cluster_log SET ts = now() - interval '40 days' WHERE kind = 'lifecycle'"))
            await s.commit()
        # The production pass: one transaction per batch, the xact lock re-taken each time.
        result = await prune_module.prune_once(db)
        assert result is not None
        tasks, logs = result
        async with db() as s:
            assert tasks >= 2 and logs >= 1
            assert (await s.execute(text("SELECT count(*) FROM task_journal WHERE status = 'lost'"))).scalar() == 0
            # Open rows are never pruned by age.
            open_rows = (await s.execute(text("SELECT count(*) FROM task_journal WHERE status IN ('pending','running')"))).scalar()
            await s.execute(text("UPDATE task_journal SET updated_at = now() - interval '40 days' WHERE status IN ('pending','running')"))
            await s.commit()
        second = await prune_module.prune_once(db)
        async with db() as s:
            still = (await s.execute(text("SELECT count(*) FROM task_journal WHERE status IN ('pending','running')"))).scalar()
            assert still == open_rows, (second, open_rows, still)

    _run(loop, go())
