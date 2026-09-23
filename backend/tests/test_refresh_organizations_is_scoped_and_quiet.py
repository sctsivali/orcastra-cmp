"""refresh-organizations is scoped to the caller and keeps the fetched body out of the log.

It took `api_key_id` from the query string with no overlap check, so a Partner could make the
server call any key's stored callback URL with that key's credential hash. It also logged the
upstream body at ERROR and echoed the upstream status to the caller.

important: the scope is read on the event loop. run_blocking uses run_in_executor, which does
not carry ContextVars, so a check made inside the thread sees None and passes everyone. The
scope tests below run the real run_blocking, so moving the check into the thread fails them.
"""

import json
import logging
from typing import Any, Dict, List

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core import org_scope
from app.core.safe_http import GuardedResponse, SafeHTTPError
from app.core.url_safety import UnsafeURLError
from app.routers import integrations

KEY_B = {
    "id": "oak_test_b",
    "cluster_permissions": [{"cluster_id": "cluster-b"}],
    "app_url": "https://example.test",
}


class _NoRawHttpx:
    def __init__(self, *_a: Any, **_k: Any) -> None:
        raise AssertionError("the refresh must call out through guarded_request, not raw httpx")


class Outbound:
    """Records every outbound call the refresh makes and answers with a fixed response."""

    def __init__(self, status: int = 200, body: Any = None, text: str = "", raises=None) -> None:
        self.status = status
        self.body = body
        self.text = text
        self.raises = raises
        self.calls: List[Dict[str, Any]] = []

    def install(self, monkeypatch) -> None:
        async def recorder(method: str, url: str, **kwargs: Any) -> GuardedResponse:
            self.calls.append({"method": method, "url": url, **kwargs})
            if self.raises is not None:
                raise self.raises
            if self.body is not None:
                return GuardedResponse(self.status, "application/json", json.dumps(self.body).encode())
            return GuardedResponse(self.status, "text/plain", self.text.encode())

        monkeypatch.setattr(integrations, "guarded_request", recorder)
        monkeypatch.setattr(httpx, "AsyncClient", _NoRawHttpx)


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


async def test_the_callback_goes_out_under_the_url_policy(key, monkeypatch):
    outbound = Outbound(body={"organizations": []})
    outbound.install(monkeypatch)
    await _refresh(None)
    call = outbound.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == "https://example.test/api/external/organizations"
    assert call["max_redirects"] == 0
    assert call["require_https"] is True
    assert call["content_type_prefix"] == "application/json"
    assert call["max_bytes"] == integrations.REFRESH_RESPONSE_MAX_BYTES


async def test_a_refused_url_is_a_502_naming_the_policy(key, monkeypatch):
    Outbound(raises=UnsafeURLError("host resolves to an internal address")).install(monkeypatch)
    with pytest.raises(HTTPException) as caught:
        await _refresh(None)
    assert caught.value.status_code == 502
    assert caught.value.detail == "The stored callback URL is not allowed"


async def test_a_transport_failure_is_a_502(key, monkeypatch):
    Outbound(raises=SafeHTTPError("could not reach the URL")).install(monkeypatch)
    with pytest.raises(HTTPException) as caught:
        await _refresh(None)
    assert caught.value.status_code == 502
    assert caught.value.detail == "Failed to reach external app"


async def test_a_planted_loopback_value_is_refused_before_any_connection(key, monkeypatch):
    """A value stored before the boundary check existed is re-checked at use time."""
    key["metadata"]["app_url"] = "http://127.0.0.1:4050"
    monkeypatch.setattr(httpx.AsyncClient, "send", _refuse_send)
    with pytest.raises(HTTPException) as caught:
        await _refresh(None)
    assert caught.value.status_code == 502
    assert caught.value.detail == "The stored callback URL is not allowed"


async def _refuse_send(*_a: Any, **_k: Any) -> None:
    raise AssertionError("no connection may be attempted for a refused URL")


async def test_a_body_that_is_not_json_is_a_502(key, monkeypatch):
    Outbound(status=200, text="<html>").install(monkeypatch)
    with pytest.raises(HTTPException) as caught:
        await _refresh(None)
    assert caught.value.status_code == 502


@pytest.mark.parametrize(
    "state",
    [
        {"is_revoked": True},
        {"is_active": False},
        {"expires_at": "2020-01-01T00:00:00"},
        {"expires_at": "not-a-date"},
    ],
    ids=["revoked", "inactive", "expired", "unreadable-expiry"],
)
async def test_an_unusable_key_is_refused_and_nothing_is_fetched(key, monkeypatch, state):
    key["metadata"] = {**KEY_B, **state}
    outbound = Outbound(body={"organizations": [{"id": "org-1", "name": "Org", "slug": "org"}]})
    outbound.install(monkeypatch)
    with pytest.raises(HTTPException) as caught:
        await _refresh({"cluster-b"})
    assert caught.value.status_code == 409
    assert outbound.calls == [], "a revoked key's callback URL must never be called"


async def test_a_key_that_has_not_expired_yet_proceeds(key, monkeypatch):
    key["metadata"] = {**KEY_B, "is_active": True, "expires_at": "2999-01-01T00:00:00"}
    outbound = Outbound(body={"organizations": []})
    outbound.install(monkeypatch)
    await _refresh({"cluster-b"})
    assert len(outbound.calls) == 1
