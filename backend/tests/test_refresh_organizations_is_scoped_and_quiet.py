"""refresh-organizations is scoped to the caller and keeps the fetched body out of the log.

It took `api_key_id` from the query string with no overlap check, so a Partner could make the
server call any key's stored callback URL with that key's credential hash. It also logged the
upstream body at ERROR and echoed the upstream status to the caller.

important: the scope is read on the event loop. run_blocking uses run_in_executor, which does
not carry ContextVars, so a check made inside the thread sees None and passes everyone. The
scope tests below run the real run_blocking, so moving the check into the thread fails them.
"""

import logging
from typing import Any, Dict, List

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core import org_scope
from app.routers import integrations

KEY_B = {
    "id": "oak_test_b",
    "cluster_permissions": [{"cluster_id": "cluster-b"}],
    "app_url": "https://example.test",
}


class Outbound:
    """Records every outbound call the refresh makes and answers with a fixed response."""

    def __init__(self, status: int = 200, body: Any = None, text: str = "") -> None:
        self.status = status
        self.body = body
        self.text = text
        self.calls: List[Dict[str, Any]] = []

    def install(self, monkeypatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append({"url": str(request.url), "headers": dict(request.headers)})
            if self.body is not None:
                return httpx.Response(self.status, json=self.body)
            return httpx.Response(self.status, text=self.text)

        real = httpx.AsyncClient

        def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(handler)
            return real(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)


@pytest.fixture
def key(monkeypatch):
    state = {"metadata": dict(KEY_B)}
    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: object())
    monkeypatch.setattr(
        integrations, "get_api_key_from_vault", lambda _v, _k: (dict(state["metadata"]), "h")
    )
    return state


async def _refresh(allowed, key_id: str = "oak_test_b"):
    token = org_scope._allowed_clusters_var.set(allowed)
    try:
        return await integrations.refresh_organizations(api_key_id=key_id, db=None)
    finally:
        org_scope._allowed_clusters_var.reset(token)


async def test_a_partner_without_overlap_gets_404_and_nothing_is_fetched(key, monkeypatch):
    outbound = Outbound(body={"organizations": []})
    outbound.install(monkeypatch)
    with pytest.raises(HTTPException) as caught:
        await _refresh({"cluster-a"})
    assert caught.value.status_code == 404
    assert outbound.calls == [], "an out-of-scope key's callback URL must never be called"


async def test_a_partner_with_overlap_proceeds(key, monkeypatch):
    outbound = Outbound(body={"organizations": []})
    outbound.install(monkeypatch)
    result = await _refresh({"cluster-b"})
    assert result["organizations_synced"] == 0
    assert len(outbound.calls) == 1


async def test_an_admin_proceeds(key, monkeypatch):
    outbound = Outbound(body={"organizations": []})
    outbound.install(monkeypatch)
    await _refresh(None)
    assert len(outbound.calls) == 1


async def test_an_upstream_error_is_not_echoed_or_logged(key, monkeypatch, caplog):
    outbound = Outbound(status=500, text="BODY-MARKER")
    outbound.install(monkeypatch)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(HTTPException) as caught:
        await _refresh(None)
    assert caught.value.status_code == 502
    assert "BODY-MARKER" not in str(caught.value.detail)
    assert "500" not in str(caught.value.detail)
    assert "BODY-MARKER" not in caplog.text


async def test_a_vault_stall_answers_503(key, monkeypatch):
    from app.core.blocking import BlockingTimeout

    async def _stall(*_a: Any, **_k: Any) -> None:
        raise BlockingTimeout("refresh", 1.0)

    monkeypatch.setattr(integrations, "run_blocking", _stall)
    with pytest.raises(HTTPException) as caught:
        await _refresh(None)
    assert caught.value.status_code == 503


def _client() -> TestClient:
    from app.core.auth import AuthenticatedUser
    from app.core.database import get_db
    from app.core.rbac import get_current_user

    app = FastAPI()
    app.include_router(integrations.router)
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        user_id="partner", email="partner@example.test", groups=["role_partner"]
    )
    app.dependency_overrides[integrations.org_scope_dependency] = lambda: None
    app.dependency_overrides[get_db] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("bad", ["../x", "a/b", "a.b", "a%2Fb", "x" * 129])
def test_a_key_id_outside_the_pattern_is_422(bad, key):
    response = _client().post("/integrations/refresh-organizations", params={"api_key_id": bad})
    assert response.status_code == 422, response.text[:200]


def test_a_real_key_id_passes_the_pattern(key, monkeypatch):
    outbound = Outbound(body={"organizations": []})
    outbound.install(monkeypatch)
    response = _client().post(
        "/integrations/refresh-organizations",
        params={"api_key_id": "oak_0123456789abcdef0123456789abcdef"},
    )
    assert response.status_code == 200, response.text[:200]
