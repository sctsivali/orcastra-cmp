"""Opening a console or a shell needs the same tenant `view` the instance's reads and power need.

Power, metrics, the single read, capabilities, the attached ISO and active sessions all require
tenant `view` on the instance whatever the permission flag says. The console session create,
the console socket and the terminal socket checked only the project bound, plus `console`
behind the flag. With the flag off, a tenant holding any rule on the cluster could open a shell
into an instance it could neither power nor see in its list. `console` itself stays flag-gated.
"""

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from app.core.config import get_settings
from app.routers import console, terminal
from tests.instance_access_fakes import CLUSTER, tenant

UNVIEWABLE = {("default", "other"): {"view", "console"}}
VIEWABLE = {("default", "web01"): {"view"}}
USER = SimpleNamespace(email="tenant@example.test", user_id="t1", groups=["role_tenant"])


class _NoDb:
    async def __aenter__(self) -> "_NoDb":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _ConsoleService:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create_console_session(self, **kwargs: Any) -> tuple:
        self.calls.append(kwargs)
        return {}, None


@pytest.fixture(autouse=True)
def flag_off() -> None:
    assert not get_settings().tenant_perm_enforcement_enabled, "these cases pin the flag-off path"


@pytest.fixture
def break_glass(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate that follows authorization, refusing, so a test can see it was reached."""
    from app.core import database
    from app.services import break_glass_service

    async def refuse(*args: Any, **kwargs: Any) -> None:
        raise HTTPException(status_code=403, detail="break_glass_required")

    monkeypatch.setattr(break_glass_service, "enforce_sensitive_access", refuse)
    monkeypatch.setattr(database, "async_session_maker", lambda: _NoDb())
    monkeypatch.setattr(console, "async_session_maker", lambda: _NoDb())


@pytest.fixture
def console_service(monkeypatch: pytest.MonkeyPatch) -> _ConsoleService:
    service = _ConsoleService()
    monkeypatch.setattr(console, "get_console_service", lambda: service)
    return service


def _session() -> Any:
    return console.create_console_session(
        instance_name="web01", host=CLUSTER, project="default", console_type="vga",
        width=1024, height=768, _rbac=USER,
    )


async def test_a_console_session_on_an_unviewable_instance_is_refused(break_glass, console_service):
    with tenant(UNVIEWABLE):
        with pytest.raises(HTTPException) as exc:
            await _session()
    assert exc.value.status_code == 403
    assert "view" in exc.value.detail
    assert console_service.calls == []


async def test_a_console_session_on_a_viewable_instance_reaches_break_glass(break_glass, console_service):
    with tenant(VIEWABLE):
        with pytest.raises(HTTPException) as exc:
            await _session()
    assert exc.value.detail == "break_glass_required"


class FakeWebSocket:
    def __init__(self) -> None:
        self.accepted = False
        self.closed: Any = None

    async def accept(self, **kwargs: Any) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)


def _tenant_socket(monkeypatch: pytest.MonkeyPatch, module: Any, rules: dict) -> None:
    async def set_scope(user: Any) -> None:
        from app.core import org_scope

        org_scope._allowed_clusters_var.set({CLUSTER})
        org_scope._tenant_acl_var.set({CLUSTER: rules})
        org_scope._visible_projects_var.set(None)

    monkeypatch.setattr(module, "set_org_scope_for_ws", set_scope)


def _console_ws(ws: FakeWebSocket) -> Any:
    return console.console_websocket(
        websocket=ws, instance_name="web01", host=CLUSTER, project="default",
        console_type="vga", user=USER,
    )


def _terminal_ws(ws: FakeWebSocket) -> Any:
    return terminal.terminal_websocket(
        websocket=ws, instance_name="web01", host=CLUSTER, project="default", user=USER
    )


SOCKETS = [(console, _console_ws), (terminal, _terminal_ws)]


@pytest.mark.parametrize("module,opener", SOCKETS, ids=["console", "terminal"])
async def test_a_socket_to_an_unviewable_instance_closes_before_accept(
    module, opener, monkeypatch, break_glass
):
    _tenant_socket(monkeypatch, module, UNVIEWABLE)
    ws = FakeWebSocket()
    await opener(ws)
    assert ws.closed == (1008, "forbidden")
    assert not ws.accepted


@pytest.mark.parametrize("module,opener", SOCKETS, ids=["console", "terminal"])
async def test_a_socket_to_a_viewable_instance_reaches_break_glass(
    module, opener, monkeypatch, break_glass
):
    _tenant_socket(monkeypatch, module, VIEWABLE)
    ws = FakeWebSocket()
    await opener(ws)
    assert ws.closed == (1008, "break_glass_required")
    assert not ws.accepted
