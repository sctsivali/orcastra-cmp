"""Durable audit delivery for accepted and terminal instance operations."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects import postgresql

from app.models.db_models import InstanceOperationAuditOutbox, InstanceOperationJob
from app.services import instance_operation_audit as audit
from app.services import instance_specifications
from app.services.lxd import instance as instance_module


def _job(*, kind: str = "specification", status: str = "success") -> SimpleNamespace:
    return SimpleNamespace(
        job_id="job-123",
        kind=kind,
        cluster_id="cluster-a",
        project="project-a",
        instance_name="vm-a",
        instance_type="virtual-machine",
        requester="actor-123",
        target_organization_id=17,
        target_organization_slug="org-seventeen",
        target_organization_resolution="exact",
        requested=(
            {
                "instance_type": "virtual-machine",
                "profiles": ["base"],
                "start_on_create": True,
            }
            if kind == "create"
            else {"cpu_count": 4, "memory_bytes": 4 * 1024**3}
        ),
        apply_mode="graceful_restart" if kind == "specification" else None,
        status=status,
        operation_id="operation-start",
        operation_ids=["operation-create", "operation-start"],
        error=None if status == "success" else "sanitized failure",
        before_spec={"cpu_count": 2},
        result={"specification": {"cpu_count": 4}},
    )


def _event(stage: str) -> InstanceOperationAuditOutbox:
    return InstanceOperationAuditOutbox(
        event_id=audit.instance_operation_audit_event_id("job-123", stage),
        job_id="job-123",
        stage=stage,
    )


def test_event_identity_is_stable_and_stage_specific():
    accepted = audit.instance_operation_audit_event_id("job-123", "accepted")
    assert accepted == audit.instance_operation_audit_event_id("job-123", "accepted")
    assert accepted != audit.instance_operation_audit_event_id("job-123", "terminal")
    assert len(accepted) == 64


@pytest.mark.parametrize(
    ("kind", "action"),
    [
        ("create", "instance.create"),
        ("specification", "instance.specification.update.accepted"),
    ],
)
def test_accepted_audit_is_rebuilt_from_the_safe_job_projection(kind, action):
    values = audit._audit_values(_event("accepted"), _job(kind=kind))

    assert values["action"] == action
    assert values["result"] == "accepted"
    assert values["event_id"] == audit.instance_operation_audit_event_id(
        "job-123", "accepted"
    )
    assert values["target_project"] == "project-a"
    assert values["target_organization_id"] == 17
    serialized = repr(values)
    assert "cloud-init" not in serialized
    assert "password" not in serialized
    assert "private_key" not in serialized


def test_terminal_audit_keeps_safe_before_after_and_organization_snapshot():
    values = audit._audit_values(
        _event("terminal"),
        _job(kind="specification", status="partial"),
    )

    assert values["action"] == "instance.specification.update"
    assert values["result"] == "failure"
    assert values["error_code"] == "PARTIAL"
    assert values["before_state"] == {"cpu_count": 2}
    assert values["after_state"] == {"specification": {"cpu_count": 4}}
    assert values["target_organization_slug"] == "org-seventeen"
    assert values["details"]["operation_ids"] == [
        "operation-create",
        "operation-start",
    ]


def test_legacy_create_keeps_its_audit_marker_without_a_second_event():
    job = _job(kind="create")
    job.requested["legacy_synchronous"] = True

    values = audit._audit_values(_event("accepted"), job)

    assert values["details"]["legacy_api"] is True
    assert values["event_id"] == audit.instance_operation_audit_event_id(
        job.job_id, "accepted"
    )


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Scalar:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _Session:
    def __init__(self, rows):
        self.rows = rows
        self.commits = 0
        self.statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, query):
        self.statements.append(query)
        return _Rows(self.rows)

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_failed_emit_stays_pending_and_replay_uses_the_same_event_id(monkeypatch):
    event = _event("terminal")
    job = _job(kind="create", status="success")
    session = _Session([(event, job)])
    calls = []

    class Logger:
        fail = True

        def log_activity(self, **values):
            calls.append(values["event_id"])
            if self.fail:
                raise RuntimeError("audit transport offline; token=redacted")

    logger = Logger()
    monkeypatch.setattr(audit, "async_session_maker", lambda: session)
    monkeypatch.setattr(audit, "get_activity_logger", lambda: logger)

    assert await audit.drain_instance_operation_audits(job_id=job.job_id) == 0
    assert event.delivered_at is None
    assert event.attempts == 1
    assert event.last_error

    logger.fail = False
    assert await audit.drain_instance_operation_audits(job_id=job.job_id) == 1
    assert event.delivered_at is not None
    assert event.attempts == 2
    assert event.last_error is None
    assert calls == [event.event_id, event.event_id]
    assert session.commits == 2
    sql = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "EXISTS" in sql
    assert "accepted" in sql
    assert "SKIP LOCKED" in sql


@pytest.mark.asyncio
async def test_failed_accepted_emit_blocks_the_same_jobs_terminal_event(monkeypatch):
    accepted = _event("accepted")
    terminal = _event("terminal")
    job = _job(kind="create", status="success")
    session = _Session([(accepted, job), (terminal, job)])
    calls = []

    class Logger:
        def log_activity(self, **values):
            calls.append(values["event_id"])
            if values["result"] == "accepted":
                raise RuntimeError("accepted audit transport offline")

    monkeypatch.setattr(audit, "async_session_maker", lambda: session)
    monkeypatch.setattr(audit, "get_activity_logger", lambda: Logger())

    assert await audit.drain_instance_operation_audits(job_id=job.job_id) == 0
    assert calls == [accepted.event_id]
    assert accepted.attempts == 1
    assert accepted.delivered_at is None
    assert terminal.attempts is None
    assert terminal.delivered_at is None


class _CreateJobSession:
    def __init__(self, *, fail_commit: bool = False):
        self.fail_commit = fail_commit
        self.trace = []
        self.pending = []
        self.rollback_snapshot = []
        self.execute_count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, _query):
        self.execute_count += 1
        self.trace.append(f"execute:{self.execute_count}")
        return _Scalar()

    def add(self, row):
        self.pending.append(row)
        self.trace.append(f"add:{type(row).__name__}")

    async def flush(self):
        self.trace.append("flush")

    async def commit(self):
        self.trace.append("commit")
        if self.fail_commit:
            raise IntegrityError("insert", {}, Exception("foreign key violation"))
        self.pending.clear()

    async def rollback(self):
        self.trace.append("rollback")
        self.rollback_snapshot = [type(row).__name__ for row in self.pending]
        self.pending.clear()

    async def refresh(self, _row):
        self.trace.append("refresh")


def _create_job_kwargs():
    return {
        "kind": "create",
        "host": "cluster-a",
        "project": "project-a",
        "instance_name": "vm-a",
        "requester": "actor-123",
        "idempotency_key": "request-123",
        "payload_hash": "payload-123",
        "requested": {"profiles": ["default"], "start_on_create": True},
    }


@pytest.mark.asyncio
async def test_create_job_flushes_parent_before_enqueuing_accepted_audit(monkeypatch):
    session = _CreateJobSession()
    monkeypatch.setattr(instance_specifications, "async_session_maker", lambda: session)
    monkeypatch.setattr(instance_specifications.secrets, "token_hex", lambda _size: "job-123")

    job, created = await instance_specifications.create_operation_job(
        **_create_job_kwargs()
    )

    assert created is True
    assert isinstance(job, InstanceOperationJob)
    assert session.trace == [
        "execute:1",
        "execute:2",
        "add:InstanceOperationJob",
        "flush",
        "add:InstanceOperationAuditOutbox",
        "commit",
        "refresh",
    ]


@pytest.mark.asyncio
async def test_create_job_commit_failure_rolls_back_parent_and_audit_pointer(monkeypatch):
    session = _CreateJobSession(fail_commit=True)
    monkeypatch.setattr(instance_specifications, "async_session_maker", lambda: session)
    monkeypatch.setattr(instance_specifications.secrets, "token_hex", lambda _size: "job-123")

    with pytest.raises(instance_specifications.SpecificationConflict):
        await instance_specifications.create_operation_job(**_create_job_kwargs())

    assert session.trace == [
        "execute:1",
        "execute:2",
        "add:InstanceOperationJob",
        "flush",
        "add:InstanceOperationAuditOutbox",
        "commit",
        "rollback",
        "execute:3",
    ]
    assert session.rollback_snapshot == [
        "InstanceOperationJob",
        "InstanceOperationAuditOutbox",
    ]
    assert session.pending == []


def test_job_state_and_outbox_pointer_share_each_commit_boundary():
    create_source = inspect.getsource(instance_specifications.create_operation_job)
    set_source = inspect.getsource(instance_specifications._set_job)
    spec_terminal_source = inspect.getsource(instance_specifications._terminal)
    create_terminal_source = inspect.getsource(instance_module._finish_create_job)

    assert create_source.index("await db.flush()") < create_source.index(
        "enqueue_instance_operation_audit"
    ) < create_source.index("await db.commit()")
    assert set_source.index("enqueue_instance_operation_audit") < set_source.index(
        "await db.commit()"
    )
    assert 'audit_stage="terminal"' in spec_terminal_source
    assert create_terminal_source.index(
        "enqueue_instance_operation_audit"
    ) < create_terminal_source.index("await db.commit()")


def test_outbox_model_enforces_one_event_per_job_stage():
    constraints = {
        constraint.name
        for constraint in InstanceOperationAuditOutbox.__table__.constraints
        if constraint.name
    }
    assert "uq_instance_operation_audit_job_stage" in constraints
    assert "ck_instance_operation_audit_stage" in constraints
