"""A terminal or console socket that is refused tells the browser why, in a close it can read.

A connect-time refusal accepts the socket and then closes it with a code and a reason token.
uvicorn answers a close before accept() with HTTP 403, which the browser sees as 1006 with no
reason, so a page opened from a deep link could not tell an expired ticket from a dropped network.
Starlette's TestClient hides the difference (it reports a close before accept as
WebSocketDisconnect(1008)), so the last test runs uvicorn on loopback with a real client.
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, WebSocket

from app.core import database, org_scope, ws_close
from app.core.config import get_settings
from app.routers import console, terminal
from app.services import break_glass_service
from app.services.migration_fence import MigrationFenceConflict
from tests._loopback_server import LoopbackServer


class _Socket:
    """What the endpoints touch on a socket: its offered subprotocols, its state, accept and close."""

    def __init__(self, subprotocols=None, refusal=None):
        self.scope = {"subprotocols": list(subprotocols or [])}
        self.state = SimpleNamespace()
        if refusal is not None:
            self.state.ws_refusal = refusal
        self.events = []

    async def accept(self, subprotocol=None, headers=None):
        self.events.append(("accept", subprotocol))

    async def close(self, code=1000, reason=None):
        self.events.append(("close", code, reason))


USER = SimpleNamespace(email="ops@example.test", user_id="ops@example.test", groups=["role_admin"])


class _Db:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


@pytest.fixture
def no_instance_io(monkeypatch):
    """Every gate passes unless a test says otherwise, and nothing may reach the instance."""

    async def keep_scope(_user, **_kwargs):
        return None

    def no_proxy(*_args, **_kwargs):
        pytest.fail("a refused socket reached the instance")

    async def no_grant_needed(_user, _host, _db):
        return None

    async def no_org(_host, _db):
        return None, None

    async def not_fenced(**_kwargs):
        return None

    for module in (terminal, console):
        monkeypatch.setattr(module, "set_org_scope_for_ws", keep_scope)
        monkeypatch.setattr(module, "assert_instance_not_migration_fenced", not_fenced)
    monkeypatch.setattr(terminal, "TerminalProxy", no_proxy)
    monkeypatch.setattr(console, "ConsoleProxy", no_proxy)
    monkeypatch.setattr(terminal, "verify_instance_access", lambda host, project, name, op: project)
    monkeypatch.setattr(console, "_verify_instance_access", lambda host, project, name, op: project)
    monkeypatch.setattr(break_glass_service, "enforce_sensitive_access", no_grant_needed)
    monkeypatch.setattr(org_scope, "resolve_cluster_org", no_org)
    monkeypatch.setattr(database, "async_session_maker", _Db)
    settings = get_settings()
    original = settings.auth_enabled
    object.__setattr__(settings, "auth_enabled", True)
    yield monkeypatch
    object.__setattr__(settings, "auth_enabled", original)


def _open(handler, sock, user=USER, console_type="vga"):
    kwargs = {"websocket": sock, "instance_name": "web-1", "host": "c1", "project": "default", "user": user}
    if handler is console.console_websocket:
        kwargs["console_type"] = console_type
    asyncio.run(handler(**kwargs))
    return sock.events


HANDLERS = [terminal.terminal_websocket, console.console_websocket]


@pytest.mark.parametrize("handler", HANDLERS)
@pytest.mark.parametrize(
    "recorded, expected",
    [
        (None, (4401, "auth_required")),
        ((4401, "ticket_invalid"), (4401, "ticket_invalid")),
        ((4401, "session_ended"), (4401, "session_ended")),
        ((4401, "token_refused"), (4401, "token_refused")),
        ((4403, "forbidden"), (4403, "forbidden")),
    ],
)
def test_a_caller_who_could_not_be_identified_hears_why(no_instance_io, handler, recorded, expected):
    events = _open(handler, _Socket(refusal=recorded), user=None)

    assert events == [("accept", None), ("close", *expected)]


@pytest.mark.parametrize("handler", HANDLERS)
def test_a_foreign_instance_is_refused_as_forbidden(no_instance_io, handler):
    def refuse(*_args):
        raise HTTPException(status_code=403, detail="forbidden")

    no_instance_io.setattr(terminal, "verify_instance_access", refuse)
    no_instance_io.setattr(console, "_verify_instance_access", refuse)

    assert _open(handler, _Socket()) == [("accept", None), ("close", 4403, "forbidden")]


@pytest.mark.parametrize("handler", HANDLERS)
def test_a_missing_break_glass_grant_is_its_own_close(no_instance_io, handler):
    async def no_grant(_user, _host, _db):
        raise HTTPException(status_code=403, detail={"error": "break_glass_required"})

    no_instance_io.setattr(break_glass_service, "enforce_sensitive_access", no_grant)

    assert _open(handler, _Socket()) == [("accept", None), ("close", 4428, "break_glass_required")]


@pytest.mark.parametrize("handler", HANDLERS)
def test_a_fenced_instance_is_its_own_close(no_instance_io, handler):
    async def fenced(**_kwargs):
        raise MigrationFenceConflict("Migration Center owns this instance")

    no_instance_io.setattr(terminal, "assert_instance_not_migration_fenced", fenced)
    no_instance_io.setattr(console, "assert_instance_not_migration_fenced", fenced)

    assert _open(handler, _Socket()) == [("accept", None), ("close", 4423, "migration_fenced")]


def test_an_unknown_console_type_is_refused_once_the_caller_is_known(no_instance_io):
    events = _open(console.console_websocket, _Socket(), console_type="rdp")

    assert events == [("accept", None), ("close", 1008, "unknown_console_type")]


def test_an_anonymous_caller_with_an_unknown_console_type_hears_only_the_sign_in_refusal(no_instance_io):
    events = _open(console.console_websocket, _Socket(), user=None, console_type="rdp")

    assert events == [("accept", None), ("close", 4401, "auth_required")]


def test_a_spice_channel_is_refused_on_the_subprotocol_it_offered(no_instance_io):
    """spice-html5 fails a socket that does not echo "binary", which would hide the reason."""
    events = _open(console.console_websocket, _Socket(subprotocols=["binary"]), user=None)

    assert events == [("accept", "binary"), ("close", 4401, "auth_required")]


def test_every_reason_is_a_short_ascii_token():
    for code, reasons in ws_close.REASONS.items():
        for reason in reasons:
            assert reason == ws_close.reason_token(reason), (code, reason)
            assert len(reason.encode("ascii")) <= ws_close.MAX_REASON_BYTES
    assert ws_close.reason_token("Instance fenced by Migration Center!") == "instance_fenced_by_migration_center_"
    assert len(ws_close.reason_token("x" * 200)) == 64


def test_a_socket_already_accepted_is_only_closed():
    from starlette.websockets import WebSocketState

    sock = _Socket()
    sock.application_state = WebSocketState.CONNECTED

    asyncio.run(ws_close.refuse(sock, ws_close.FORBIDDEN, "access_revoked:access_ended"))

    assert sock.events == [("close", 4403, "access_revoked:access_ended")]


def test_a_client_that_left_does_not_raise():
    class _Gone(_Socket):
        async def accept(self, subprotocol=None, headers=None):
            raise RuntimeError("disconnected")

    asyncio.run(ws_close.refuse(_Gone(), ws_close.AUTH, "auth_required"))


def test_a_break_glass_grant_that_ends_closes_its_session_with_4428(monkeypatch):
    class _Gone:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _model, _grant_id):
            return None

    monkeypatch.setattr(database, "async_session_maker", _Gone)
    sock = _Socket()

    asyncio.run(break_glass_service.watch_grant_while_open(7, sock, poll_seconds=0.01))

    assert sock.events == [("close", 4428, "break_glass_expired")]


@pytest.mark.parametrize("module_name", ["terminal", "console"])
def test_no_endpoint_closes_a_socket_it_has_not_accepted(module_name):
    """The source shape the rule rests on: every refusal goes through ws_close.refuse."""
    import inspect
    from importlib import import_module

    module = import_module(f"app.routers.{module_name}")
    handler = module.terminal_websocket if module_name == "terminal" else module.console_websocket
    source = inspect.getsource(handler)

    assert "websocket.close(" not in source
    assert source.count("ws_close.refuse(") >= 4


# ── the real thing: uvicorn on loopback, a real client ───────────────────────────────────────


def test_uvicorn_answers_a_close_before_accept_with_403_and_a_refused_terminal_with_4401(no_instance_io):
    from websockets.exceptions import ConnectionClosed, InvalidStatus
    from websockets.sync.client import connect

    app = FastAPI()

    @app.websocket("/closed-before-accept")
    async def closed_before_accept(websocket: WebSocket):
        await websocket.close(code=1008, reason="Authentication required")

    app.include_router(terminal.router)
    app.dependency_overrides[get_settings] = lambda: SimpleNamespace(
        auth_enabled=True, authentik_issuer="https://idp.invalid/", ws_query_token_enabled=False,
    )

    with LoopbackServer(app) as server:
        base = f"ws://127.0.0.1:{server.port}"

        with pytest.raises(InvalidStatus) as refused:
            connect(f"{base}/closed-before-accept", open_timeout=5)
        assert refused.value.response.status_code == 403, "what the browser reports as a bare 1006"

        with connect(f"{base}/terminal/ws/web-1?host=c1&project=default&ticket=spent", open_timeout=5) as client:
            with pytest.raises(ConnectionClosed) as closed:
                client.recv(timeout=5)
        assert (closed.value.rcvd.code, closed.value.rcvd.reason) == (4401, "ticket_invalid")
