"""Console session discovery and cancellation stay inside the authorized project.

An operation id is cluster-wide while an instance name is only project-wide. These tests keep
the HTTP layer's project gate, operation-resource match, and organization session projection
together so a shared cluster cannot turn either endpoint into an IDOR or an inventory oracle.
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Response

from app.core import org_scope
from app.core.blocking import BlockingPoolFull
from app.routers import console
from app.services import lxd_service as lxd_service_module


HOST = "shared-cluster"
PROJECT = "customer-a"
INSTANCE = "web-01"


class _Response:
    def __init__(self, status_code, metadata=None):
        self.status_code = status_code
        self._payload = {"metadata": metadata}

    def json(self):
        return self._payload


class _OperationEndpoint:
    def __init__(self, metadata=None):
        self.metadata = metadata
        self.get_calls = []
        self.delete_calls = []

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        return _Response(200, self.metadata)

    def delete(self, **kwargs):
        self.delete_calls.append(kwargs)
        return _Response(202, {})


class _UncertainOperationEndpoint(_OperationEndpoint):
    def delete(self, **kwargs):
        self.delete_calls.append(kwargs)
        return _Response(500, {})

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        if len(self.get_calls) == 1:
            return _Response(200, self.metadata)
        raise TimeoutError("verification timed out")


class _Operations:
    def __init__(self, rows):
        self.rows = rows
        self.list_calls = []
        self.endpoints = {
            row["id"]: _OperationEndpoint(row)
            for row in rows
            if row.get("id")
        }

    def get(self, **kwargs):
        self.list_calls.append(kwargs)
        return _Response(200, {"running": self.rows})

    def __getitem__(self, operation_id):
        return self.endpoints[operation_id]


def _operation(operation_id, name=INSTANCE, project=PROJECT, operation_class="websocket"):
    query = f"?project={project}" if project else ""
    return {
        "id": operation_id,
        "class": operation_class,
        "project": project or None,
        "description": f"Console for {name}",
        "resources": {"instances": [f"/1.0/instances/{name}{query}"]},
        "created_at": "2026-09-19T00:00:00Z",
        "may_cancel": True,
    }


def _install_service(monkeypatch, rows):
    operations = _Operations(rows)
    client = SimpleNamespace(api=SimpleNamespace(operations=operations))
    service = SimpleNamespace(
        connection_manager=SimpleNamespace(get_client=lambda _host: client)
    )
    monkeypatch.setattr(lxd_service_module, "get_lxd_service", lambda: service)

    async def immediate_console_io(call, *, label):
        del label
        return call()

    # These unit tests exercise route ownership and response semantics. The production helper's
    # executor contract is asserted separately below; running threads in this sandbox prevents
    # asyncio's cross-thread wake-up pipe from signalling completion.
    monkeypatch.setattr(console, "_console_io", immediate_console_io)
    return operations


@pytest.mark.parametrize(
    ("operation", "instance_name", "project", "expected"),
    [
        (_operation("exact"), INSTANCE, PROJECT, True),
        (_operation("prefix", name=f"{INSTANCE}-copy"), INSTANCE, PROJECT, False),
        (_operation("foreign", project="customer-b"), INSTANCE, PROJECT, False),
        (_operation("task", operation_class="task"), INSTANCE, PROJECT, False),
        (_operation("default", project=""), INSTANCE, "default", True),
        (_operation("unknown-project", project=""), INSTANCE, PROJECT, False),
    ],
)
def test_operation_match_requires_exact_instance_and_project(
    operation, instance_name, project, expected
):
    assert console._operation_targets_instance(
        operation,
        instance_name=instance_name,
        project=project,
    ) is expected


def test_kill_all_only_deletes_exact_project_resources(monkeypatch):
    rows = [
        _operation("mine"),
        _operation("same-name-foreign", project="customer-b"),
        _operation("prefix", name=f"{INSTANCE}-copy"),
        _operation("ordinary-task", operation_class="task"),
    ]
    operations = _install_service(monkeypatch, rows)
    monkeypatch.setattr(console, "verify_project_access", lambda host, project: PROJECT)

    result = asyncio.run(
        console.kill_all_sessions(
            INSTANCE,
            host=HOST,
            project=PROJECT,
            _=None,
            _rbac=None,
        )
    )

    assert result.success is True
    assert operations.list_calls == [
        {"params": {"recursion": "1", "project": PROJECT}, "timeout": (5, 30)}
    ]
    assert len(operations.endpoints["mine"].delete_calls) == 1
    assert operations.endpoints["mine"].delete_calls[0]["params"] == {"project": PROJECT}
    assert not operations.endpoints["same-name-foreign"].delete_calls
    assert not operations.endpoints["prefix"].delete_calls
    assert not operations.endpoints["ordinary-task"].delete_calls


def test_kill_all_reports_a_matching_session_that_refused_cancellation(monkeypatch):
    rows = [_operation("refused")]
    operations = _install_service(monkeypatch, rows)
    operations.endpoints["refused"] = _UncertainOperationEndpoint(rows[0])
    monkeypatch.setattr(console, "verify_project_access", lambda host, project: PROJECT)

    result = asyncio.run(
        console.kill_all_sessions(
            INSTANCE,
            host=HOST,
            project=PROJECT,
            _=None,
            _rbac=None,
        )
    )

    assert result.success is False
    assert result.error == "Killed 0 console session(s), but 1 session(s) could not be terminated"


def test_kill_one_reads_and_verifies_metadata_before_delete(monkeypatch):
    rows = [_operation("foreign", project="customer-b")]
    operations = _install_service(monkeypatch, rows)
    monkeypatch.setattr(console, "verify_project_access", lambda host, project: PROJECT)

    with pytest.raises(HTTPException) as denied:
        asyncio.run(
            console.kill_active_session(
                INSTANCE,
                "foreign",
                host=HOST,
                project=PROJECT,
                _=None,
                _rbac=None,
            )
        )

    assert denied.value.status_code == 404
    endpoint = operations.endpoints["foreign"]
    assert endpoint.get_calls == [
        {"params": {"project": PROJECT}, "timeout": (5, 30)}
    ]
    assert not endpoint.delete_calls


def test_kill_one_repeats_project_scope_on_delete(monkeypatch):
    rows = [_operation("mine")]
    operations = _install_service(monkeypatch, rows)
    monkeypatch.setattr(console, "verify_project_access", lambda host, project: PROJECT)

    result = asyncio.run(
        console.kill_active_session(
            INSTANCE,
            "mine",
            host=HOST,
            project=PROJECT,
            _=None,
            _rbac=None,
        )
    )

    assert result.success is True
    assert operations.endpoints["mine"].delete_calls == [
        {"params": {"project": PROJECT}, "timeout": (5, 30)}
    ]


def test_kill_one_does_not_claim_success_when_follow_up_verification_times_out(monkeypatch):
    rows = [_operation("uncertain")]
    operations = _install_service(monkeypatch, rows)
    operations.endpoints["uncertain"] = _UncertainOperationEndpoint(rows[0])
    monkeypatch.setattr(console, "verify_project_access", lambda host, project: PROJECT)

    result = asyncio.run(
        console.kill_active_session(
            INSTANCE,
            "uncertain",
            host=HOST,
            project=PROJECT,
            _=None,
            _rbac=None,
        )
    )

    assert result.success is False
    assert result.error == "Could not verify whether the session was terminated"
    assert len(operations.endpoints["uncertain"].get_calls) == 2


def test_project_denial_happens_before_lxd_lookup(monkeypatch):
    touched = False

    def _service():
        nonlocal touched
        touched = True
        raise AssertionError("LXD must not be reached")

    def _deny(_host, _project):
        raise HTTPException(status_code=403, detail="Access denied to this project")

    monkeypatch.setattr(lxd_service_module, "get_lxd_service", _service)
    monkeypatch.setattr(console, "verify_project_access", _deny)

    with pytest.raises(HTTPException) as denied:
        asyncio.run(
            console.kill_all_sessions(
                INSTANCE,
                host=HOST,
                project="customer-b",
                _=None,
                _rbac=None,
            )
        )

    assert denied.value.status_code == 403
    assert touched is False


def test_console_io_uses_the_bounded_executor(monkeypatch):
    captured = {}

    async def fake_run_blocking(call, *args, **kwargs):
        captured.update(kwargs)
        assert args == ()
        return call()

    monkeypatch.setattr(console, "run_blocking", fake_run_blocking)

    result = asyncio.run(console._console_io(lambda: "done", label="console.test"))

    assert result == "done"
    assert captured == {
        "timeout": console._CONSOLE_OPERATION_TIMEOUT_SECONDS,
        "label": "console.test",
        "executor": console._console_operation_executor,
        "max_in_flight": console._CONSOLE_OPERATION_MAX_IN_FLIGHT,
    }


def test_active_session_saturation_returns_503(monkeypatch):
    async def saturated(*_args, **_kwargs):
        raise BlockingPoolFull("console.operations.connect", 8)

    monkeypatch.setattr(console, "_console_io", saturated)
    monkeypatch.setattr(
        console,
        "_verify_instance_access",
        lambda host, project, instance, permission: PROJECT,
    )

    with pytest.raises(HTTPException) as denied:
        asyncio.run(
            console.check_active_sessions(
                INSTANCE,
                host=HOST,
                project=PROJECT,
                _rbac=None,
            )
        )

    assert denied.value.status_code == 503


def _session(name, project):
    data = {
        "session_id": name,
        "instance_name": name,
        "host": HOST,
        "project": project,
    }
    return SimpleNamespace(**data, to_dict=lambda: dict(data))


def test_org_session_view_applies_project_and_instance_view_boundaries(monkeypatch):
    sessions = [
        _session("visible", PROJECT),
        _session("no-view", PROJECT),
        _session("foreign-project", "customer-b"),
    ]

    class _Tracker:
        async def list_active(self, **kwargs):
            assert kwargs["allowed_clusters"] == {HOST}
            assert kwargs["user_email"] is None
            return sessions, False

    monkeypatch.setattr(console, "get_session_tracker", lambda: _Tracker())
    monkeypatch.setattr(org_scope, "get_current_allowed_clusters", lambda: {HOST})
    monkeypatch.setattr(
        console,
        "project_is_visible",
        lambda _host, project: project == PROJECT,
    )
    monkeypatch.setattr(
        console,
        "check_tenant_permission",
        lambda _host, _project, instance, permission: (
            permission == "view" and instance == "visible"
        ),
    )

    rows = asyncio.run(
        console.list_active_tracked_sessions(
            response=Response(),
            instance_name=None,
            host=None,
            scope="org",
            _rbac=SimpleNamespace(email="partner@example.test"),
        )
    )

    assert [row["session_id"] for row in rows] == ["visible"]


class _AuditResult:
    def __init__(self, *, scalar=None, rows=None):
        self._scalar = scalar
        self._rows = rows or []

    def scalar_one(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _AuditDB:
    def __init__(self):
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        if "count(" in str(statement).lower():
            return _AuditResult(scalar=0)
        return _AuditResult(rows=[])


class _AuditMaker:
    def __init__(self, db):
        self.db = db

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *_args):
        return False


def test_org_audit_count_and_page_share_project_and_instance_sql_boundaries(monkeypatch):
    db = _AuditDB()
    monkeypatch.setattr(console, "async_session_maker", _AuditMaker(db))
    monkeypatch.setattr(org_scope, "get_current_allowed_clusters", lambda: {HOST})
    monkeypatch.setattr(
        org_scope,
        "get_current_visible_projects",
        lambda: {HOST: {PROJECT}},
    )
    monkeypatch.setattr(
        org_scope,
        "get_tenant_acl",
        lambda: {
            HOST: {
                (PROJECT, "visible"): {"view"},
                (PROJECT, "operate-only"): {"operate"},
            }
        },
    )

    page = asyncio.run(
        console.query_session_audit_logs(
            instance_name=None,
            event_type=None,
            scope="org",
            limit=25,
            offset=0,
            _rbac=SimpleNamespace(email="tenant@example.test"),
        )
    )

    assert page["total"] == 0
    assert len(db.statements) == 2
    count_where = str(db.statements[0].whereclause)
    page_where = str(db.statements[1].whereclause)
    assert count_where == page_where
    assert "session_audit_logs.host" in count_where
    assert "session_audit_logs.project" in count_where
    assert "session_audit_logs.instance_name" in count_where


def test_audit_without_an_email_is_refused_before_database_access(monkeypatch):
    touched = False

    class _NeverEnter:
        def __call__(self):
            nonlocal touched
            touched = True
            raise AssertionError("database must not be reached")

    monkeypatch.setattr(console, "async_session_maker", _NeverEnter())

    with pytest.raises(HTTPException) as denied:
        asyncio.run(
            console.query_session_audit_logs(
                instance_name=None,
                event_type=None,
                scope="self",
                limit=25,
                offset=0,
                _rbac=SimpleNamespace(email=None),
            )
        )

    assert denied.value.status_code == 403
    assert touched is False
