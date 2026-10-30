"""Race-closure checks for migration-sensitive mobility and interactive work."""

from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from app.models.migration_schemas import MigrationPlanCreateRequest
from app.routers import (
    image_uploads,
    image_uploads_dashboard,
    instance_mobility,
    iso_uploads,
    iso_uploads_dashboard,
)
from app.services.migration_fence import (
    MIGRATION_CLUSTER_FENCE_UNAVAILABLE_DETAIL,
    MigrationFenceConflict,
)
from app.services.migrations import planning, preflight as migration_preflight
from app.services.migrations.plan_contracts import approval_report_hash


def _migration_request() -> MigrationPlanCreateRequest:
    return MigrationPlanCreateRequest.model_validate(
        {
            "scope": "instance",
            "intent": "move",
            "source": {
                "cluster_id": "cluster-z",
                "project": "source-project",
                "resource_name": "workload",
            },
            "target": {
                "cluster_id": "cluster-a",
                "project": "target-project",
                "resource_name": "workload-copy",
            },
            "mappings": {"storage_pools": {"root": "fast"}},
        }
    )


class _FirstResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def first(self) -> Any:
        return self.value


class _OrderedDatabase:
    def __init__(self, *results: Any) -> None:
        self.results = list(results)
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _FirstResult:
        self.statements.append(statement)
        # Unscripted planner checks (OrcaHub ownership, active endpoints) find nothing.
        return _FirstResult(self.results.pop(0) if self.results else None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("results", "expected_table"),
    [
        (("image-import",), "image_upload_sessions"),
        ((None, ("iso-import",)), "iso_upload_sessions"),
    ],
)
async def test_migration_preflight_blocks_active_or_unknown_upload_rows(
    results: tuple[Any, ...],
    expected_table: str,
) -> None:
    database = _OrderedDatabase(*results)

    assert await planning._has_active_upload_session(
        database,  # type: ignore[arg-type]
        _migration_request(),
    )

    assert expected_table in str(database.statements[-1])
    compiled_values = {
        item
        for statement in database.statements
        for value in statement.compile().params.values()
        for item in (value if isinstance(value, (list, tuple)) else (value,))
    }
    assert {"pending", "uploading", "uploaded", "importing", "unknown"}.issubset(
        compiled_values
    )
    assert {"cluster-z", "cluster-a", "source-project", "target-project"}.issubset(
        compiled_values
    )


@pytest.mark.asyncio
async def test_active_upload_is_an_approval_relevant_preflight_blocker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _OrderedDatabase(None, None, ("image-import",))
    service = planning.MigrationPlanningMixin(
        db=database,  # type: ignore[arg-type]
        lxd_service=object(),
        settings=SimpleNamespace(),
    )
    result = migration_preflight.PreflightResult(
        strategy="staged_cold",
        downtime="required",
        capabilities={
            "source": {"engine": "lxd"},
            "target": {"engine": "lxd"},
            "checkpoint": True,
            "rollback": True,
            "live": False,
            "incremental_refresh": False,
        },
    )

    async def clients(*_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        return object(), object()

    async def inspect_preflight(*_args: Any, **_kwargs: Any) -> Any:
        return result

    monkeypatch.setattr(service, "_clients", clients)
    monkeypatch.setattr(planning, "_blocking", inspect_preflight)

    report = await service._preflight(
        _migration_request(),
        source_fingerprint="source-fingerprint",
        target_fingerprint="target-fingerprint",
    )

    assert [blocker["code"] for blocker in report["blockers"]] == [
        "active_upload_session"
    ]
    assert report["approval_hash"] == approval_report_hash(report)


@pytest.mark.asyncio
async def test_legacy_copy_flag_off_refuses_before_any_daemon_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = instance_mobility.get_settings()
    old_center = settings.migration_center_enabled
    old_copy = settings.migration_durable_copy_enabled
    object.__setattr__(settings, "migration_center_enabled", False)
    object.__setattr__(settings, "migration_durable_copy_enabled", False)

    async def must_not_open_daemon(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("disabled durable copy reached a daemon")

    monkeypatch.setattr(instance_mobility, "_client", must_not_open_daemon)
    monkeypatch.setattr(
        instance_mobility,
        "verify_project_access",
        lambda _host, value: value,
    )

    body = instance_mobility.MoveRequest(
        host="cluster-z",
        project="source-project",
        target_host="cluster-a",
        target_project="target-project",
        target_name="workload-copy",
    )
    try:
        with pytest.raises(HTTPException) as exc:
            await instance_mobility.copy_instance(
                instance_name="workload",
                body=body,
                lxd_service=object(),
                idempotency_key=None,
                db=object(),  # type: ignore[arg-type]
                _=None,
                user=SimpleNamespace(email="operator@example.test", user_id="operator"),
            )
    finally:
        object.__setattr__(settings, "migration_center_enabled", old_center)
        object.__setattr__(settings, "migration_durable_copy_enabled", old_copy)

    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "durable_copy_admission_disabled"


@pytest.mark.asyncio
async def test_legacy_copy_flag_on_delegates_to_the_durable_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = instance_mobility.get_settings()
    old_center = settings.migration_center_enabled
    old_copy = settings.migration_durable_copy_enabled
    object.__setattr__(settings, "migration_center_enabled", True)
    object.__setattr__(settings, "migration_durable_copy_enabled", True)
    source = object()
    delegated: list[dict[str, Any]] = []

    async def client(_service: Any, host: str) -> object:
        assert host == "cluster-z"
        return source

    async def policy(client: Any, name: str, project: str) -> dict[str, Any]:
        assert (client, name, project) == (source, "workload", "source-project")
        return {}

    async def durable(**kwargs: Any) -> instance_mobility.MoveResponse:
        delegated.append(kwargs)
        return instance_mobility.MoveResponse(
            success=True,
            instance_name="workload",
            target_host="cluster-a",
            target_project="target-project",
            target_name="workload-copy",
            transport="durable",
            migration_job_id="job-copy",
            message="Durable copy accepted",
        )

    monkeypatch.setattr(instance_mobility, "_client", client)
    monkeypatch.setattr(instance_mobility, "_read_source_policy", policy)
    monkeypatch.setattr(
        instance_mobility,
        "verify_project_access",
        lambda _host, value: value,
    )
    monkeypatch.setattr(instance_mobility, "_durable_copy", durable)

    body = instance_mobility.MoveRequest(
        host="cluster-z",
        project="source-project",
        target_host="cluster-a",
        target_project="target-project",
        target_name="workload-copy",
    )
    try:
        response = await instance_mobility.copy_instance(
            instance_name="workload",
            body=body,
            lxd_service=object(),
            idempotency_key="copy-key",
            db=object(),  # type: ignore[arg-type]
            _=None,
            user=SimpleNamespace(email="operator@example.test", user_id="operator"),
        )
    finally:
        object.__setattr__(settings, "migration_center_enabled", old_center)
        object.__setattr__(settings, "migration_durable_copy_enabled", old_copy)

    assert response.migration_job_id == "job-copy"
    assert len(delegated) == 1
    assert delegated[0]["idempotency_key"] == "copy-key"


class _ImportDatabase:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [image_uploads, iso_uploads])
async def test_upload_worker_preserves_staging_when_admission_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
) -> None:
    database = _ImportDatabase()
    row = SimpleNamespace(
        session_id="upload-a",
        cluster_id="cluster-a",
        project="project-a",
        staging_path="/staging/upload-a",
        status="importing",
        error=None,
        expected_sha256=None,
        alias="image-a",
        pool="pool-a",
        volume_name="iso-a",
    )
    cleaned: list[str] = []

    @asynccontextmanager
    async def session_scope():
        yield database

    @asynccontextmanager
    async def blocked(*_args: Any, **_kwargs: Any):
        raise MigrationFenceConflict(MIGRATION_CLUSTER_FENCE_UNAVAILABLE_DETAIL)
        yield  # pragma: no cover

    async def get_session(_db: Any, _session_id: str) -> Any:
        return row

    async def heartbeat(_session_id: str) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    monkeypatch.setattr(module, "async_session_maker", session_scope)
    monkeypatch.setattr(module, "_get_session", get_session)
    monkeypatch.setattr(module, "_heartbeat", heartbeat)
    monkeypatch.setattr(module, "guard_project_mutation", blocked)
    monkeypatch.setattr(module, "_cleanup_file", cleaned.append)

    await module._run_import("upload-a")

    assert row.status == "uploaded"
    assert row.error == MIGRATION_CLUSTER_FENCE_UNAVAILABLE_DETAIL
    assert database.rollbacks == 1
    assert database.commits == 1
    assert cleaned == []


@pytest.mark.asyncio
@pytest.mark.parametrize("module", [image_uploads, iso_uploads])
async def test_upload_worker_holds_project_admission_through_daemon_settlement(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
) -> None:
    held = False
    events: list[str] = []
    row = SimpleNamespace(
        session_id="upload-a",
        cluster_id="cluster-a",
        project="project-a",
        staging_path="/staging/upload-a",
        status="importing",
        error=None,
        expected_sha256=None,
        alias="image-a",
        pool="pool-a",
        volume_name="iso-a",
        fingerprint=None,
    )

    @asynccontextmanager
    async def session_scope():
        yield object()

    @asynccontextmanager
    async def guarded(*_args: Any, **_kwargs: Any):
        nonlocal held
        held = True
        events.append("guard:enter")
        try:
            yield
        finally:
            held = False
            events.append("guard:exit")

    async def get_session(_db: Any, _session_id: str) -> Any:
        return row

    async def no_fence(**_kwargs: Any) -> None:
        assert held
        events.append("fence:checked")

    async def heartbeat(_session_id: str) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    def daemon_import(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        assert held
        events.append("daemon:settled")
        return {"fingerprint": "fingerprint-a", "size": 1}

    async def publish(_db: Any, target: Any, _result: Any) -> None:
        assert held
        target.status = "ready"
        target.fingerprint = "fingerprint-a"
        events.append("row:ready")

    def cleanup(_path: str) -> None:
        assert held is False
        events.append("staging:cleaned")

    monkeypatch.setattr(module, "async_session_maker", session_scope)
    monkeypatch.setattr(module, "_get_session", get_session)
    monkeypatch.setattr(module, "_heartbeat", heartbeat)
    monkeypatch.setattr(module, "guard_project_mutation", guarded)
    monkeypatch.setattr(module, "assert_project_not_migration_fenced", no_fence)
    monkeypatch.setattr(module, "_publish_ready", publish)
    monkeypatch.setattr(module, "_cleanup_file", cleanup)
    if module is image_uploads:
        monkeypatch.setattr(module, "_lxd_import_sync", daemon_import)
    else:
        monkeypatch.setattr(module, "_lxd_import_iso_sync", daemon_import)

    await module._run_import("upload-a")

    assert events == [
        "guard:enter",
        "fence:checked",
        "daemon:settled",
        "row:ready",
        "guard:exit",
        "staging:cleaned",
    ]


def test_every_upload_admission_boundary_uses_the_shared_project_guard() -> None:
    for module in (
        image_uploads,
        iso_uploads,
        image_uploads_dashboard,
        iso_uploads_dashboard,
    ):
        assert "async with guard_project_mutation(" in inspect.getsource(
            module.create_upload
        )
        assert "async with guard_project_mutation(" in inspect.getsource(
            module.finalize_upload
        )


class _Tracker:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def register(self, *, session_id: str, **_kwargs: Any) -> Any:
        self.events.append("tracker:register")
        return SimpleNamespace(session_id=session_id)

    def mark_connected(self, _session_id: str) -> None:
        self.events.append("tracker:connected")

    def unregister(self, _session_id: str) -> None:
        self.events.append("tracker:unregister")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["console", "terminal"])
async def test_interactive_session_releases_admission_before_websocket_lifetime(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    from app.services import console_proxy, terminal_proxy

    held = False
    events: list[str] = []
    tracker = _Tracker(events)

    @asynccontextmanager
    async def guarded(**_kwargs: Any):
        nonlocal held
        held = True
        events.append("guard:enter")
        try:
            yield
        finally:
            held = False
            events.append("guard:exit")

    class _Browser:
        def __init__(self) -> None:
            self.state = SimpleNamespace()

        async def receive(self) -> dict[str, Any]:
            return {"type": "websocket.receive", "text": '{"width": 120, "height": 40, "binary": true}'}

        async def send_json(self, _payload: Any) -> None:
            assert held is False
            events.append("browser:message")

        async def send_text(self, _payload: str) -> None:
            events.append("browser:error")

        async def close(self, **_kwargs: Any) -> None:
            events.append("browser:close")

    class _Service:
        def open_console_operation(self, *_args: Any) -> dict[str, str]:
            assert held
            events.append("daemon:submit")
            return {"operation_id": "console-op", "control_url": "wss://cluster/control", "ws_url": "wss://cluster/data"}

        def open_exec_operation(self, *_args: Any) -> dict[str, str]:
            assert held
            events.append("daemon:submit")
            return {"operation_id": "terminal-op", "control_url": "wss://cluster/control", "ws_url": "wss://cluster/data"}

    def material(_host: str) -> Any:
        assert held is False
        events.append("websocket:begin")
        raise RuntimeError("stop after admission")

    async def inline(fn: Any, *args: Any, label: str, host: str, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)

    class _Activity:
        def log_activity(self, **_kwargs: Any) -> None:
            pass

    service = _Service()
    browser = _Browser()
    module = console_proxy if kind == "console" else terminal_proxy
    monkeypatch.setattr(module, "guard_instance_mutation", guarded)
    monkeypatch.setattr(module, "get_session_tracker", lambda: tracker)
    monkeypatch.setattr(module, "session_io", inline)
    monkeypatch.setattr(module, "connection_material", material)
    if kind == "console":
        monkeypatch.setattr(console_proxy, "get_console_service", lambda: service)
        monkeypatch.setattr(console_proxy, "get_activity_logger", lambda: _Activity())
        proxy = console_proxy.ConsoleProxy(browser, "workload", "cluster-a", "project-a")
    else:
        monkeypatch.setattr(terminal_proxy, "get_terminal_service", lambda: service)
        proxy = terminal_proxy.TerminalProxy(browser, "workload", "cluster-a", "project-a")

    await proxy.run()

    assert events.index("daemon:submit") < events.index("guard:exit")
    assert events.index("guard:exit") < events.index("websocket:begin")
    assert "tracker:register" in events
