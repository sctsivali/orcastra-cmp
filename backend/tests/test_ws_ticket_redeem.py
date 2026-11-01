"""A socket opens with a ticket of its own kind, for its own target, under a live session, once.

Each refusal below is a way a ticket could otherwise be worth more than one socket: replayed,
spent on another kind of socket, pointed at another instance, or used after the session that
minted it ended. The old ?token= stays accepted only while WS_QUERY_TOKEN_ENABLED says so.
"""

import asyncio
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core import auth as auth_module
from app.core.auth import AuthenticatedUser
from app.core.config import get_settings
from app.core.ws_auth import ws_user
from app.core.ws_ticket import WsTarget, claims_for
from app.core.ws_ticket_store import get_ws_ticket_store


def _settings(tokens: bool = False) -> SimpleNamespace:
    return SimpleNamespace(auth_enabled=True, authentik_issuer="https://idp.invalid/", ws_query_token_enabled=tokens)


def _user(name: str = "redeem-admin", *, lifetime: float = 300.0) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=f"{name}@example.test", email=f"{name}@example.test", groups=["role_admin"],
        roles=["role_admin"], raw_claims={"sub": name}, session_exp=time.time() + lifetime,
    )


def _ticket(kind: str, target: WsTarget = WsTarget(), user: AuthenticatedUser = None) -> str:
    claims = claims_for(kind, target, user or _user())
    return asyncio.run(get_ws_ticket_store().mint(claims.user_id, claims.to_json()))


def _socket(query: dict, path: dict = None) -> SimpleNamespace:
    # `state` as on a Starlette WebSocket: the redeem leaves the SPICE display group there.
    return SimpleNamespace(query_params=query, path_params=path or {}, state=SimpleNamespace())


def _open(kind: str, socket: SimpleNamespace, tokens: bool = False):
    return asyncio.run(ws_user(kind)(socket, _settings(tokens)))


TERMINAL = WsTarget(host="c1", project="shop", instance="web-1")


def _terminal_socket(ticket: str, **query) -> SimpleNamespace:
    return _socket({"ticket": ticket, "host": "c1", "project": "shop", **query}, {"instance_name": "web-1"})


def test_a_ticket_opens_its_socket_once_as_its_caller():
    ticket = _ticket("terminal", TERMINAL)

    first = _open("terminal", _terminal_socket(ticket))
    second = _open("terminal", _terminal_socket(ticket))

    assert first is not None and first.user_id == "redeem-admin@example.test"
    assert first.raw_claims == {"sub": "redeem-admin"} and first.session_exp > time.time()
    assert second is None, "a spent ticket opens nothing"


@pytest.mark.parametrize(
    "socket_kind, query, path",
    [
        ("console", {"host": "c1", "project": "shop", "console_type": "vga"}, {"instance_name": "web-1"}),
        ("terminal", {"host": "c2", "project": "shop"}, {"instance_name": "web-1"}),
        ("terminal", {"host": "c1", "project": "other"}, {"instance_name": "web-1"}),
        ("terminal", {"host": "c1"}, {"instance_name": "web-1"}),
        ("terminal", {"host": "c1", "project": "shop"}, {"instance_name": "web-2"}),
    ],
)
def test_a_ticket_opens_nothing_but_its_own_kind_and_target(socket_kind, query, path):
    ticket = _ticket("terminal", TERMINAL)

    assert _open(socket_kind, _socket({"ticket": ticket, **query}, path)) is None
    assert _open("terminal", _terminal_socket(ticket)) is None, "spent by the refused attempt"


def test_a_console_ticket_is_bound_to_its_console_type():
    ticket = _ticket("console", WsTarget(host="c1", project="shop", instance="web-1", console_type="console"))
    graphics = _socket({"ticket": ticket, "host": "c1", "project": "shop"}, {"instance_name": "web-1"})

    assert _open("console", graphics) is None, "the socket defaults to vga"


def test_a_ticket_whose_session_ended_opens_nothing():
    # Past the 30 s clock-skew leeway (AUTH_JWT_LEEWAY_SECONDS); test_a_clock_a_little_ahead covers inside it.
    ticket = _ticket("tasks", user=_user(lifetime=-60))

    assert _open("tasks", _socket({"ticket": ticket})) is None


def test_no_ticket_opens_nothing():
    assert _open("tasks", _socket({})) is None


def test_the_old_token_works_only_while_it_is_enabled(monkeypatch):
    monkeypatch.setattr(auth_module, "validate_jwt", lambda token, settings: _user("legacy"))
    socket = _socket({"token": "jwt"})

    assert _open("tasks", socket, tokens=True).user_id == "legacy@example.test"
    assert _open("tasks", socket, tokens=False) is None


def test_a_url_still_carrying_a_token_is_refused_even_with_a_good_ticket():
    ticket = _ticket("tasks")

    assert _open("tasks", _socket({"ticket": ticket, "token": "jwt"}), tokens=False) is None
    assert _open("tasks", _socket({"ticket": ticket})) is None, "and the ticket is spent"


def test_a_spent_ticket_written_back_after_its_lifetime_opens_nothing(monkeypatch):
    """Someone who can read and write Redis replays a captured signed entry under its own key."""
    from app.core.ws_ticket import ticket_digest
    from app.core.ws_ticket_store import TICKET_PREFIX

    store = get_ws_ticket_store()
    ticket = _ticket("tasks")
    key = f"{TICKET_PREFIX}:{ticket_digest(ticket)}"
    captured = store._local[key][1]
    assert _open("tasks", _socket({"ticket": ticket})) is not None

    store._local[key] = (time.monotonic() + 30, captured)
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 40)

    assert _open("tasks", _socket({"ticket": ticket})) is None, "minted 40 s ago, past 30 s and the skew"


@pytest.fixture
def monitoring_app(monkeypatch):
    from app.routers import monitoring

    app = FastAPI()
    app.include_router(monitoring.ws_router)
    app.dependency_overrides[get_settings] = lambda: _settings()
    monkeypatch.setattr("app.core.config.get_settings", lambda: _settings())
    return TestClient(app)


def test_a_feed_closes_with_4401_when_its_session_ends(monitoring_app):
    ticket = _ticket("monitoring", user=_user("feed-admin", lifetime=1.5))

    with monitoring_app.websocket_connect(f"/monitoring/ws?ticket={ticket}") as socket:
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_text()

    assert closed.value.code == 4401


def test_a_replayed_feed_ticket_is_refused_at_the_handshake(monitoring_app):
    ticket = _ticket("monitoring", user=_user("feed-admin-2", lifetime=1.0))
    with monitoring_app.websocket_connect(f"/monitoring/ws?ticket={ticket}") as socket:
        with pytest.raises(WebSocketDisconnect):
            socket.receive_text()

    with pytest.raises(WebSocketDisconnect) as refused:
        with monitoring_app.websocket_connect(f"/monitoring/ws?ticket={ticket}") as socket:
            socket.receive_text()

    assert refused.value.code == 1008


def test_the_token_query_is_refused_unless_an_operator_turns_it_back_on():
    from app.core.config import Settings

    assert Settings.model_fields["ws_query_token_enabled"].default is False
    assert Settings.model_fields["ws_query_token_enabled"].alias == "WS_QUERY_TOKEN_ENABLED"
