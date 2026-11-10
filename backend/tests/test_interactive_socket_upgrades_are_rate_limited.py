"""Terminal and console socket upgrades have a per-client-IP budget, refused without accepting.

The HTTP limiter never sees a WebSocket, and a refused interactive socket is accepted and then
closed so the page can read why. uvicorn holds such a socket until the client answers the close,
inside the worker's --limit-concurrency, so without a budget one client could hold those slots with
sockets that never authenticate. Over budget the upgrade is refused before accept, which holds
nothing: 429 with Retry-After where the server offers the denial-response extension.
"""

import asyncio
import logging
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, WebSocket

from app.core.config import Settings
from app.middleware.security import InteractiveSocketLimitMiddleware
from tests._loopback_server import LoopbackServer

TERMINAL = "/api/v1/terminal/ws/web-1"
CONSOLE = "/api/v1/console/web-1/ws"
DENIAL = {"websocket.http.response": {}}


def _settings(limit: int = 3, **over) -> SimpleNamespace:
    base = dict(
        rate_limit_enabled=True,
        ws_upgrade_rate_limit=limit,
        rate_limit_window_seconds=60,
        trusted_proxy_cidrs=Settings.model_fields["trusted_proxy_cidrs"].default,
        allow_private_forwarded_ips=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def one_worker(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "1")


class _Upgrade:
    """One upgrade through the middleware: what reached the app, and what the client was sent."""

    def __init__(self, middleware, path, ip, headers=(), extensions=DENIAL, kind="websocket"):
        self.reached = False
        self.sent = []

        async def app(scope, receive, send):
            self.reached = True

        middleware.app = app
        scope = {
            "type": kind,
            "path": path,
            "query_string": b"ticket=t",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
            "client": (ip, 50000),
            "extensions": extensions,
        }
        if kind == "http":
            scope["method"] = "GET"

        async def receive():
            return {"type": "websocket.connect"}

        async def send(message):
            self.sent.append(message)

        asyncio.run(middleware(scope, receive, send))

    @property
    def refused_status(self):
        return next((m["status"] for m in self.sent if m["type"] == "websocket.http.response.start"), None)


def _middleware(**settings) -> InteractiveSocketLimitMiddleware:
    return InteractiveSocketLimitMiddleware(app=None, settings=_settings(**settings))


def test_upgrades_under_the_budget_pass():
    limiter = _middleware(limit=3)

    upgrades = [_Upgrade(limiter, TERMINAL if i % 2 else CONSOLE, "203.0.113.5") for i in range(3)]

    assert all(u.reached and not u.sent for u in upgrades)


def test_the_upgrade_past_the_budget_is_refused_with_429_before_accept():
    limiter = _middleware(limit=3)
    for _ in range(3):
        _Upgrade(limiter, CONSOLE, "203.0.113.5")

    refused = _Upgrade(limiter, TERMINAL, "203.0.113.5")

    assert refused.reached is False
    assert refused.refused_status == 429
    start = refused.sent[0]
    headers = dict(start["headers"])
    assert 1 <= int(headers[b"retry-after"]) <= 60
    assert not any(m["type"] == "websocket.accept" for m in refused.sent), "nothing is held open"


def test_without_the_denial_extension_it_closes_before_accept():
    limiter = _middleware(limit=1)
    _Upgrade(limiter, TERMINAL, "203.0.113.5", extensions={})

    refused = _Upgrade(limiter, TERMINAL, "203.0.113.5", extensions={})

    assert refused.reached is False
    assert refused.sent == [{"type": "websocket.close", "code": 1013, "reason": "rate_limited"}]


def test_each_client_ip_has_its_own_budget():
    limiter = _middleware(limit=2)
    for _ in range(2):
        _Upgrade(limiter, TERMINAL, "203.0.113.5")

    assert _Upgrade(limiter, TERMINAL, "203.0.113.5").reached is False
    assert _Upgrade(limiter, TERMINAL, "198.51.100.7").reached is True


def test_behind_a_trusted_proxy_the_forwarded_client_is_counted():
    limiter = _middleware(limit=1)
    proxy = "172.18.0.2"  # the edge proxy's container address, trusted by the default TRUSTED_PROXY_CIDRS
    # Public addresses: a documentation range (203.0.113.0/24) counts as private, and a private
    # forwarded address is ignored unless ALLOW_PRIVATE_FORWARDED_IPS says otherwise.
    first, second = "93.184.215.15", "93.184.215.14"

    assert _Upgrade(limiter, TERMINAL, proxy, headers=[("CF-Connecting-IP", first)]).reached
    assert not _Upgrade(limiter, TERMINAL, proxy, headers=[("CF-Connecting-IP", first)]).reached
    assert _Upgrade(limiter, TERMINAL, proxy, headers=[("CF-Connecting-IP", second)]).reached, (
        "two clients behind one proxy are two budgets"
    )


def test_an_untrusted_client_cannot_spread_itself_over_forged_headers():
    limiter = _middleware(limit=1)
    direct = "93.184.215.15"  # public, so not a trusted proxy

    assert _Upgrade(limiter, TERMINAL, direct, headers=[("X-Forwarded-For", "93.184.215.14")]).reached
    assert not _Upgrade(limiter, TERMINAL, direct, headers=[("X-Forwarded-For", "93.184.215.16")]).reached


def test_feeds_and_http_requests_are_not_counted():
    limiter = _middleware(limit=1)
    _Upgrade(limiter, TERMINAL, "203.0.113.5")

    assert _Upgrade(limiter, "/api/v1/events/ws", "203.0.113.5").reached
    assert _Upgrade(limiter, "/api/v1/monitoring/ws", "203.0.113.5").reached
    assert _Upgrade(limiter, "/api/v1/terminal/health", "203.0.113.5", kind="http").reached


def test_the_figure_is_deployment_wide_and_each_worker_enforces_its_share(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    limiter = _middleware(limit=8)

    passed = sum(_Upgrade(limiter, TERMINAL, "203.0.113.5").reached for _ in range(5))

    assert passed == 2


@pytest.mark.parametrize("settings", [{"limit": 0}, {"limit": 1, "rate_limit_enabled": False}])
def test_it_can_be_turned_off(settings):
    limiter = _middleware(**settings)

    assert all(_Upgrade(limiter, TERMINAL, "203.0.113.5").reached for _ in range(5))


def test_a_client_over_budget_is_logged_once_per_window(caplog):
    limiter = _middleware(limit=1)
    with caplog.at_level(logging.WARNING, logger="app.middleware.security"):
        for _ in range(6):
            _Upgrade(limiter, TERMINAL, "203.0.113.5")
        _Upgrade(limiter, TERMINAL, "198.51.100.7")
        _Upgrade(limiter, TERMINAL, "198.51.100.7")

    warnings = [r for r in caplog.records if "over budget" in r.getMessage()]
    assert [r.client_ip for r in warnings] == ["203.0.113.5", "198.51.100.7"]


def test_the_default_leaves_room_for_reconnecting_consoles():
    field = Settings.model_fields["ws_upgrade_rate_limit"]

    assert field.alias == "WS_UPGRADE_RATE_LIMIT"
    # A console opens four sockets and a page reconnects at most five times a minute; the
    # per-user ticket budget (300 a minute) is the same figure.
    assert field.default == 300
    with pytest.raises(ValueError):
        Settings(WS_UPGRADE_RATE_LIMIT=-1)


def test_the_app_installs_it():
    from app.main import app

    assert InteractiveSocketLimitMiddleware in [m.cls for m in app.user_middleware]


def test_a_real_server_answers_an_over_budget_upgrade_with_429():
    from websockets.exceptions import InvalidStatus
    from websockets.sync.client import connect

    inner = FastAPI()

    @inner.websocket("/api/v1/terminal/ws/{name}")
    async def opened(websocket: WebSocket, name: str):
        await websocket.accept()
        await websocket.close(code=1000)

    inner.add_middleware(InteractiveSocketLimitMiddleware, settings=_settings(limit=2))

    with LoopbackServer(inner) as server:
        url = f"ws://127.0.0.1:{server.port}{TERMINAL}"
        for _ in range(2):
            with connect(url, open_timeout=5):
                pass
        with pytest.raises(InvalidStatus) as refused:
            connect(url, open_timeout=5)

    assert refused.value.response.status_code == 429
    assert refused.value.response.headers["Retry-After"]
