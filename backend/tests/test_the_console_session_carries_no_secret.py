"""A console session answer never carries the LXD websocket secrets.

GET /console/{name}/session returned the session dict the service built, including ws_url and
control_url, whose `?secret=` opens the LXD operation websocket with no further
authentication. No screen calls that route (the console and terminal use their WebSockets,
which read the secrets inside the backend), so the answer is now held to the fields that
identify the session. GET /terminal/session returned the same kind of secret for an exec
session, had no caller at all and no project check, and is gone.
"""

import json
from types import SimpleNamespace
from typing import Any

from app.routers import console, terminal
from app.services.console_service import _PUBLIC_SESSION_FIELDS, public_session_info
from tests.instance_access_fakes import CLUSTER, Recorder, scoped

SESSION = {
    "operation_id": "op-1",
    "instance_name": "web01",
    "host": CLUSTER,
    "project": "default",
    "console_type": "vga",
    "ws_url": "wss://cluster-a.example.test:8443/1.0/operations/op-1/websocket?secret=s3cr3t",
    "control_url": "wss://cluster-a.example.test:8443/1.0/operations/op-1/websocket?secret=c0ntr0l",
    "endpoint": "https://cluster-a.example.test:8443",
    "width": 1024,
    "height": 768,
}


def test_the_allowlist_is_exactly_the_identifying_fields():
    assert _PUBLIC_SESSION_FIELDS == (
        "operation_id", "instance_name", "host", "project", "console_type", "width", "height",
    )


def test_the_public_view_drops_every_secret_and_the_endpoint():
    public = public_session_info(SESSION)
    assert set(public) == set(_PUBLIC_SESSION_FIELDS)
    assert "secret" not in json.dumps(public)
    assert public_session_info(None) is None


class _NoDb:
    async def __aenter__(self) -> "_NoDb":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


async def test_the_session_route_answers_without_the_secrets(monkeypatch):
    from app.core import org_scope
    from app.services import break_glass_service

    class _Service:
        async def create_console_session(self, **kwargs: Any) -> tuple:
            return dict(SESSION), None

    async def no_grant(*args: Any, **kwargs: Any) -> None:
        return None

    async def org(*args: Any, **kwargs: Any) -> tuple:
        return 1, "org-a"

    monkeypatch.setattr(console, "get_console_service", lambda: _Service())
    monkeypatch.setattr(console, "async_session_maker", lambda: _NoDb())
    monkeypatch.setattr(console, "activity_logger", Recorder())
    monkeypatch.setattr(break_glass_service, "enforce_sensitive_access", no_grant)
    monkeypatch.setattr(org_scope, "resolve_cluster_org", org)

    with scoped():
        response = await console.create_console_session(
            instance_name="web01", host=CLUSTER, project="default", console_type="vga",
            width=1024, height=768, _rbac=SimpleNamespace(email="partner@example.test"),
        )
    body = response.model_dump_json()
    assert response.success
    for leaked in ("secret", "ws_url", "control_url", "endpoint"):
        assert leaked not in body, f"the session answer still carries {leaked}"
    assert response.session["operation_id"] == "op-1"


def test_the_terminal_session_route_is_gone():
    paths = {(getattr(r, "path", ""), tuple(sorted(getattr(r, "methods", None) or ()))) for r in terminal.router.routes}
    assert not any(path.endswith("/session") for path, _ in paths), paths
    assert not hasattr(terminal, "create_terminal_session")
