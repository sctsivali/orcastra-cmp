"""A deployment that asks for auth and cannot verify it must refuse.

`get_current_user` returned None when AUTH_ENABLED was true but AUTHENTIK_ISSUER was unset, and
every RBAC dependency reads None as dev mode and fabricates a full Admin. One missing environment
variable therefore turned an auth-enabled deployment into an open one, silently, for callers with
no token at all.
"""

import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from fastapi import Depends, FastAPI, HTTPException, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core import auth as auth_module
from app.core.config import get_settings

ROUTERS = Path(__file__).resolve().parents[1] / "app" / "routers"


def test_missing_issuer_refuses_instead_of_returning_none() -> None:
    src = inspect.getsource(auth_module.get_current_user)
    # Strip comments first: the branch explains the old behaviour in prose, and matching that
    # would fail for the wrong reason.
    code_only = "\n".join(
        line for line in src.split("\n") if not line.strip().startswith("#")
    )
    issuer_branch = code_only.split("authentik_issuer")[1].split("if not credentials")[0]
    assert "return None" not in issuer_branch, (
        "returning None here is read as dev mode by require_roles and yields a synthetic Admin"
    )
    assert "raise HTTPException" in issuer_branch, (
        "an auth-enabled server that cannot verify tokens must refuse the request"
    )


@pytest.mark.asyncio
async def test_the_refusal_is_a_server_fault_not_a_client_one() -> None:
    class _Settings:
        auth_enabled = True
        authentik_issuer = ""

    class _Req:
        class state:  # noqa: D106
            pass

    with pytest.raises(HTTPException) as exc:
        await auth_module.get_current_user(_Req(), None, _Settings())
    assert exc.value.status_code == 503, (
        f"a misconfigured server should not blame the caller's credentials, got "
        f"{exc.value.status_code}"
    )


# ---------------------------------------------------------------------------
# WebSockets: the same refusal, spoken as a close code
# ---------------------------------------------------------------------------


def _settings(**overrides: Any) -> SimpleNamespace:
    values = {
        "auth_enabled": True,
        "authentik_issuer": "https://idp.example.test/application/o/orcastra/",
        "authentik_audience": "orcastra-client",
        "auth_jwks_cache_ttl": 3600,
        "tenant_perm_enforcement_enabled": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _socket_app(settings: SimpleNamespace) -> FastAPI:
    app = FastAPI()

    @app.websocket("/ws")
    async def _ws(
        websocket: WebSocket,
        user: Optional[auth_module.AuthenticatedUser] = Depends(auth_module.get_ws_user),
    ) -> None:
        await websocket.accept()
        await websocket.send_json({"user": None if user is None else user.user_id})
        await websocket.close()

    app.dependency_overrides[get_settings] = lambda: settings
    return app


def test_the_websocket_refusal_closes_the_socket() -> None:
    client = TestClient(_socket_app(_settings(authentik_issuer="")))
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws?token=anything") as ws:
            ws.receive_json()
    assert exc.value.code == 1011, (
        "an auth-enabled server with no issuer must refuse the handshake as its own fault, "
        f"not accept it for an unauthenticated caller, got close code {exc.value.code}"
    )


def test_the_websocket_refusal_closes_the_socket_when_the_audience_is_missing() -> None:
    client = TestClient(_socket_app(_settings(authentik_audience="")))
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws?token=anything") as ws:
            ws.receive_json()
    assert exc.value.code == 1011, f"got close code {exc.value.code}"

def test_no_websocket_guard_gates_itself_on_the_issuer() -> None:
    offenders = []
    for path in sorted(ROUTERS.glob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            code = line.split("#", 1)[0]
            if "auth_enabled" in code and "authentik_issuer" in code:
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert not offenders, (
        "a socket guard that also requires the issuer disarms itself on a server with no "
        "issuer, which is exactly the server that cannot verify anyone:\n" + "\n".join(offenders)
    )


def _events_client(settings: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from app.routers import events

    monkeypatch.setattr(events, "get_settings", lambda: settings)
    app = FastAPI()
    app.include_router(events.router)
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


def test_a_real_socket_route_refuses_before_accepting(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _events_client(_settings(authentik_issuer=""), monkeypatch)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/events/ws?host=cluster-a") as ws:
            ws.receive_text()
    assert exc.value.code == 1011, f"got close code {exc.value.code}"


def test_a_configured_server_still_refuses_a_missing_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _events_client(_settings(), monkeypatch)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/events/ws?host=cluster-a") as ws:
            ws.receive_text()
    assert exc.value.code == 1008, f"got close code {exc.value.code}"


class _AcceptingBroadcaster:
    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        await ws.send_text("accepted")

    async def disconnect(self, ws: WebSocket) -> None:
        return None


def _monitoring_client(monkeypatch: pytest.MonkeyPatch, settings: SimpleNamespace) -> TestClient:
    from app.core import config as config_module
    from app.routers.monitoring import ws_router
    from app.services import ws_broadcast

    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    monkeypatch.setattr(ws_broadcast, "get_monitoring_broadcaster", lambda: _AcceptingBroadcaster())
    app = FastAPI()
    app.include_router(ws_router)
    return TestClient(app)


def test_the_monitoring_socket_refuses_a_misconfigured_server(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _monitoring_client(monkeypatch, _settings(authentik_issuer=""))
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/monitoring/ws") as ws:
            ws.receive_text()
    assert exc.value.code == 1011, f"got close code {exc.value.code}"


def test_the_monitoring_socket_refuses_a_server_without_an_audience(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _monitoring_client(monkeypatch, _settings(authentik_audience=""))
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/monitoring/ws?token=anything") as ws:
            ws.receive_text()
    assert exc.value.code == 1011, f"got close code {exc.value.code}"

@pytest.mark.asyncio
async def test_auth_disabled_still_returns_none_on_both_paths() -> None:
    disabled = _settings(auth_enabled=False, authentik_issuer="", authentik_audience="")

    class _Req:
        class state:  # noqa: D106
            pass

    assert await auth_module.get_current_user(_Req(), None, disabled) is None
    socket = SimpleNamespace(query_params={})
    assert await auth_module.get_ws_user(socket, disabled) is None


def test_auth_disabled_still_opens_the_socket() -> None:
    client = TestClient(_socket_app(_settings(auth_enabled=False, authentik_issuer="")))
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json() == {"user": None}



# ---------------------------------------------------------------------------
# No optional-auth path: it answered None, which every RBAC dependency reads as dev mode
# ---------------------------------------------------------------------------

COVERED_PRINCIPAL_SOURCES = {"get_current_user", "get_ws_user", "validate_jwt", "verify_token"}


def test_the_fail_open_optional_user_is_gone() -> None:
    from app.core import dependencies

    assert not hasattr(auth_module, "get_optional_user")
    assert "get_optional_user" not in dependencies.__all__
    assert not hasattr(dependencies, "get_optional_user")


def test_every_source_of_a_principal_is_one_the_suite_covers() -> None:
    import ast

    tree = ast.parse(Path(auth_module.__file__).read_text())
    producers = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.returns is not None
        and "AuthenticatedUser" in ast.unparse(node.returns)
    }
    assert producers <= COVERED_PRINCIPAL_SOURCES, (
        "a new function in core/auth.py hands out a principal. Cover its fail-closed behaviour "
        f"here before adding it to the set: {sorted(producers - COVERED_PRINCIPAL_SOURCES)}"
    )
    assert producers, "the scan found nothing, so it is looking at the wrong file"
