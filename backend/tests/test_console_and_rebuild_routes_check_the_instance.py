"""Console, terminal and rebuild routes authorize the instance and its project, not the cluster.

Each of these took a project from the caller and checked only the cluster. On a cluster bound
to two organizations a Partner could read another organization's console capabilities and
attached ISO, reorder its boot sequence, open its console or a shell, rebuild it, or cancel its
live console session, and a Tenant could read capabilities and active sessions of an instance
the list never showed them. The kill routes also matched an instance by substring of the
operation path, so killing `web` killed `web01`, and they took no project at all.

The project bound is unconditional throughout. It is the only isolation a Partner has on a
shared cluster, so it must not wait for the tenant permission flag.
"""

from types import SimpleNamespace
from typing import Any, Optional

import pytest
from fastapi import HTTPException

from app.core.instance_access import operation_targets
from app.routers import console, instance_rebuild, terminal
from app.services.console_service import StartupOrderEntry
from tests.instance_access_fakes import CLUSTER, FakeLXDService, partner, scoped, tenant

FOREIGN = "other-db"


class FakeConsoleService:
    """Records every call and answers with an empty but well-formed result."""

    def __init__(self, entries: Optional[list[StartupOrderEntry]] = None):
        self.calls: list[tuple[str, dict]] = []
        self.entries = entries or []

    async def get_instance_capabilities(self, **kwargs: Any) -> tuple:
        self.calls.append(("capabilities", kwargs))
        return SimpleNamespace(to_dict=lambda: {}), "stop here"

    async def get_instance_iso(self, **kwargs: Any) -> tuple:
        self.calls.append(("iso", kwargs))
        return None, None

    async def get_startup_order(self, **kwargs: Any) -> tuple:
        self.calls.append(("startup_order", kwargs))
        return self.entries, None

    async def update_startup_order(self, **kwargs: Any) -> tuple:
        self.calls.append(("update_startup_order", kwargs))
        return True, None

    async def attach_iso(self, **kwargs: Any) -> tuple:
        self.calls.append(("attach", kwargs))
        return True, "ok"

    async def eject_iso(self, **kwargs: Any) -> tuple:
        self.calls.append(("eject", kwargs))
        return True, "ok"

    async def create_console_session(self, **kwargs: Any) -> tuple:
        self.calls.append(("session", kwargs))
        return {}, None


@pytest.fixture
def console_service(monkeypatch) -> FakeConsoleService:
    service = FakeConsoleService()
    monkeypatch.setattr(console, "get_console_service", lambda: service)
    return service


def _no_break_glass(monkeypatch) -> None:
    from app.services import break_glass_service

    async def unreachable(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the break-glass gate was reached before the project bound")

    monkeypatch.setattr(break_glass_service, "enforce_sensitive_access", unreachable)


# -- reads that name one instance --------------------------------------------------------------

async def test_capabilities_of_an_unviewable_instance_are_refused(console_service):
    with tenant({("own-web", "other"): {"view"}}):
        with pytest.raises(HTTPException) as exc:
            await console.get_capabilities(
                instance_name="web01", host=CLUSTER, project="own-web", _rbac=None
            )
    assert exc.value.status_code == 403
    assert console_service.calls == []


async def test_capabilities_read_the_normalized_project(console_service):
    with tenant({("default", "web01"): {"view"}}):
        with pytest.raises(HTTPException):
            await console.get_capabilities(instance_name="web01", host=CLUSTER, project=" ", _rbac=None)
    assert console_service.calls[0][1]["project"] == "default"


async def test_the_attached_iso_of_a_foreign_project_is_refused(console_service):
    with partner({"default"}):
        with pytest.raises(HTTPException) as exc:
            await console.get_attached_iso(
                instance_name="web01", host=CLUSTER, project=FOREIGN, _rbac=None
            )
    assert exc.value.status_code == 403
    assert console_service.calls == []


async def test_the_startup_order_lists_only_viewable_instances(console_service):
    console_service.entries = [
        StartupOrderEntry(instance_name="web01", host=CLUSTER, project="own-web", priority=1),
        StartupOrderEntry(instance_name="db01", host=CLUSTER, project="own-web", priority=2),
    ]
    with tenant({("own-web", "web01"): {"view"}}):
        response = await console.get_startup_order(host=CLUSTER, project="own-web", _rbac=None)
    assert [e.instance_name for e in response.entries] == ["web01"]


async def test_the_startup_order_of_a_foreign_project_is_refused(console_service):
    with partner({"default"}):
        with pytest.raises(HTTPException) as exc:
            await console.get_startup_order(host=CLUSTER, project=FOREIGN, _rbac=None)
    assert exc.value.status_code == 403
    assert console_service.calls == []


# -- writes held to the Partner bound ----------------------------------------------------------

def _startup_update():
    return console.update_startup_order(
        request=console.StartupOrderUpdateRequest(updates=[]),
        host=CLUSTER, project=FOREIGN, _=None, _rbac=None,
    )


def _attach():
    return console.attach_iso(
        instance_name="web01", request=console.AttachISORequest(pool="p", volume="v"),
        host=CLUSTER, project=FOREIGN, _=None, _rbac=None,
    )


def _eject():
    return console.eject_iso(
        instance_name="web01", host=CLUSTER, project=FOREIGN, device_name="cdrom", _=None, _rbac=None
    )


def _session():
    return console.create_console_session(
        instance_name="web01", host=CLUSTER, project=FOREIGN, console_type="vga",
        width=1024, height=768, _rbac=None,
    )


@pytest.mark.parametrize(
    "call", [_startup_update, _attach, _eject, _session], ids=["startup", "attach", "eject", "session"]
)
async def test_a_foreign_project_write_is_refused(call, console_service, monkeypatch):
    _no_break_glass(monkeypatch)
    with partner({"default"}):
        with pytest.raises(HTTPException) as exc:
            await call()
    assert exc.value.status_code == 403
    assert console_service.calls == []


# -- console operations on a shared host -------------------------------------------------------

def _op(op_id: str, project: str, name: str = "web01") -> dict:
    query = "" if project == "default" else f"?project={project}"
    return {
        "id": op_id, "class": "websocket", "description": "Showing console",
        "created_at": "", "may_cancel": True,
        "resources": {"instances": [f"/1.0/instances/{name}{query}"]},
    }


class _Operations:
    def __init__(self, running: list[dict]):
        self.running = running
        self.deleted: list[str] = []

    def get(self, params: Optional[dict] = None, timeout: Any = None) -> Any:
        return SimpleNamespace(status_code=200, json=lambda: {"metadata": {"running": self.running}})

    def __getitem__(self, op_id: str) -> Any:
        by_id = {op["id"]: op for op in self.running}

        def get() -> Any:
            if op_id not in by_id:
                raise RuntimeError("not found")
            return SimpleNamespace(status_code=200, json=lambda: {"metadata": by_id[op_id]})

        def delete() -> Any:
            self.deleted.append(op_id)
            return SimpleNamespace(status_code=200)

        return SimpleNamespace(get=get, delete=delete)


@pytest.fixture
def host_operations(monkeypatch) -> _Operations:
    from app.services import lxd_service as lxd_service_module

    ops = _Operations([_op("op-own", "default"), _op("op-foreign", FOREIGN), _op("op-web", "default", "web")])
    client = SimpleNamespace(api=SimpleNamespace(operations=ops), operations=SimpleNamespace(get=_gone))
    service = SimpleNamespace(connection_manager=SimpleNamespace(get_client=lambda host: client))
    monkeypatch.setattr(lxd_service_module, "get_lxd_service", lambda: service)
    return ops


def _gone(op_id: str) -> None:
    raise RuntimeError("gone")


def test_an_operation_is_matched_by_project_and_name_not_by_substring():
    assert operation_targets(_op("a", FOREIGN)) == {(FOREIGN, "web01")}
    assert operation_targets(_op("a", "default", "web")) == {("default", "web")}
    assert operation_targets({"resources": {}}) == set()


async def test_kill_all_cancels_only_this_projects_session(host_operations):
    with partner({"default"}):
        response = await console.kill_all_sessions(
            instance_name="web01", host=CLUSTER, project="default", _=None, _rbac=None
        )
    assert response.success
    assert host_operations.deleted == ["op-own"]


async def test_kill_all_in_a_foreign_project_is_refused(host_operations):
    with partner({"default"}):
        with pytest.raises(HTTPException) as exc:
            await console.kill_all_sessions(
                instance_name="web01", host=CLUSTER, project=FOREIGN, _=None, _rbac=None
            )
    assert exc.value.status_code == 403
    assert host_operations.deleted == []


async def test_killing_one_session_needs_it_to_be_this_instances(host_operations):
    with partner({"default"}):
        response = await console.kill_active_session(
            instance_name="web01", operation_id="op-foreign", host=CLUSTER,
            project="default", _=None, _rbac=None,
        )
    assert not response.success
    assert host_operations.deleted == []


async def test_killing_this_instances_session_still_works(host_operations):
    with partner({"default"}):
        response = await console.kill_active_session(
            instance_name="web01", operation_id="op-own", host=CLUSTER,
            project="default", _=None, _rbac=None,
        )
    assert response.success
    assert host_operations.deleted == ["op-own"]


async def test_active_sessions_list_only_this_projects_operations(host_operations):
    with scoped():
        response = await console.check_active_sessions(
            instance_name="web01", host=CLUSTER, project="default", _rbac=None
        )
    assert [s.operation_id for s in response.sessions] == ["op-own"]


async def test_active_sessions_of_an_unviewable_instance_are_refused(host_operations):
    with tenant({("default", "other"): {"view"}}):
        with pytest.raises(HTTPException) as exc:
            await console.check_active_sessions(
                instance_name="web01", host=CLUSTER, project="default", _rbac=None
            )
    assert exc.value.status_code == 403


# -- the WebSockets ----------------------------------------------------------------------------

class FakeWebSocket:
    def __init__(self) -> None:
        self.accepted = False
        self.closed: Optional[tuple[int, str]] = None

    async def accept(self, **kwargs: Any) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)


class _NoDb:
    async def __aenter__(self) -> "_NoDb":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


def _bound_socket(monkeypatch, module) -> None:
    async def set_scope(user: Any) -> None:
        from app.core import org_scope

        org_scope._allowed_clusters_var.set({CLUSTER})
        org_scope._tenant_acl_var.set(None)
        org_scope._visible_projects_var.set({CLUSTER: {"default"}})

    monkeypatch.setattr(module, "set_org_scope_for_ws", set_scope)

    from app.core import database
    from app.services import break_glass_service

    async def refuse(*args: Any, **kwargs: Any) -> None:
        raise HTTPException(status_code=403, detail="break_glass_required")

    monkeypatch.setattr(break_glass_service, "enforce_sensitive_access", refuse)
    monkeypatch.setattr(database, "async_session_maker", lambda: _NoDb())


USER = SimpleNamespace(email="partner@example.test", user_id="u1", groups=["role_partner"])


def _console_ws(ws: FakeWebSocket, project: str):
    return console.console_websocket(
        websocket=ws, instance_name="web01", host=CLUSTER, project=project,
        console_type="vga", user=USER,
    )


def _terminal_ws(ws: FakeWebSocket, project: str):
    return terminal.terminal_websocket(
        websocket=ws, instance_name="web01", host=CLUSTER, project=project, user=USER
    )


@pytest.mark.parametrize("module,opener", [(console, _console_ws), (terminal, _terminal_ws)],
                         ids=["console", "terminal"])
async def test_a_foreign_project_socket_is_closed_before_accept(module, opener, monkeypatch):
    _bound_socket(monkeypatch, module)
    ws = FakeWebSocket()
    await opener(ws, FOREIGN)
    assert ws.closed == (1008, "forbidden")
    assert not ws.accepted


@pytest.mark.parametrize("module,opener", [(console, _console_ws), (terminal, _terminal_ws)],
                         ids=["console", "terminal"])
async def test_an_own_project_socket_gets_past_the_bound(module, opener, monkeypatch):
    """Control: the bound admits its own project, which then meets the break-glass gate."""
    _bound_socket(monkeypatch, module)
    ws = FakeWebSocket()
    await opener(ws, "default")
    assert ws.closed == (1008, "break_glass_required")


# -- rebuild -----------------------------------------------------------------------------------

async def test_a_rebuild_in_a_foreign_project_is_refused_before_the_confirm_check():
    body = instance_rebuild.RebuildRequest(
        host=CLUSTER, project=FOREIGN, mode="empty", confirm_name="wrong"
    )
    service = FakeLXDService(client=None)
    with partner({"default"}):
        with pytest.raises(HTTPException) as exc:
            await instance_rebuild.rebuild_instance(
                instance_name="web01", body=body, lxd_service=service, _=None, user=None
            )
    assert exc.value.status_code == 403
    assert service.calls == []


async def test_the_rebuild_preflight_of_an_unviewable_instance_is_refused():
    service = FakeLXDService(client=None)
    with tenant({("own-web", "other"): {"view"}}):
        with pytest.raises(HTTPException) as exc:
            await instance_rebuild.rebuild_preflight(
                instance_name="web01", host=CLUSTER, project="own-web", lxd_service=service
            )
    assert exc.value.status_code == 403
    assert service.calls == []


async def test_the_rebuild_preflight_reads_the_authorized_project():
    service = FakeLXDService(client=None)
    with partner({"default", "own-web"}):
        await instance_rebuild.rebuild_preflight(
            instance_name="web01", host=CLUSTER, project="own-web", lxd_service=service
        )
    assert service.calls == [("preflight", "own-web")]


class _Result:
    def __init__(self, row: Any):
        self._row = row

    def scalar_one_or_none(self) -> Any:
        return self._row


class _Db:
    def __init__(self, row: Any):
        self.row = row
        self.statements: list[Any] = []

    async def execute(self, stmt: Any) -> _Result:
        self.statements.append(stmt)
        return _Result(self.row)


def _job(project: str) -> Any:
    return SimpleNamespace(
        job_id="j1", instance_name="web01", cluster_id=CLUSTER, project=project, mode="empty",
        requested_image=None, old_image=None, status="ready", operation_id=None, error=None,
        attached_volumes={}, created_at=None, updated_at=None,
    )


async def test_a_job_in_a_foreign_project_reads_as_missing():
    with partner({"default"}):
        with pytest.raises(HTTPException) as exc:
            await instance_rebuild.get_rebuild_job(
                instance_name="web01", job_id="j1", db=_Db(_job(FOREIGN))
            )
        own = await instance_rebuild.get_rebuild_job(
            instance_name="web01", job_id="j1", db=_Db(_job("default"))
        )
    assert exc.value.status_code == 404
    assert own["project"] == "default"


async def test_the_latest_job_in_a_foreign_project_is_none():
    with partner({"default"}):
        foreign = await instance_rebuild.get_latest_rebuild_job(
            instance_name="web01", host=CLUSTER, project=None, db=_Db(_job(FOREIGN))
        )
        own = await instance_rebuild.get_latest_rebuild_job(
            instance_name="web01", host=CLUSTER, project=None, db=_Db(_job("default"))
        )
    assert foreign == {"job": None}
    assert own["job"]["project"] == "default"


async def test_the_latest_job_can_be_filtered_by_project():
    db = _Db(_job("default"))
    with scoped():
        await instance_rebuild.get_latest_rebuild_job(
            instance_name="web01", host=CLUSTER, project=" ", db=db
        )
    compiled = db.statements[0].compile(compile_kwargs={"literal_binds": True})
    assert "rebuild_jobs.project = 'default'" in str(compiled)


async def test_the_provisioning_wait_reads_the_rebuilt_project():
    """A same-named instance in another project must not end the wait for this one."""
    asked = []

    class _Service:
        async def get_vm(self, name: str, host: str, project: Optional[str] = None) -> Any:
            asked.append(project)
            return SimpleNamespace(status="Running", ip_addresses=["192.0.2.10"])

    await instance_rebuild._wait_for_provisioning(_Service(), "web01", CLUSTER, "own-web")
    assert asked == ["own-web"]
