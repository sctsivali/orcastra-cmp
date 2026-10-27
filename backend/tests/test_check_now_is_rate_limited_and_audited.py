"""An Administrator's "Check now" reads the feed at most once a minute across every worker, and
leaves an audit record either way (routers/release_status.py)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException, Response

from app.core import config
from app.routers import release_status
from app.services.release_check import service
from app.services.release_check.store import ReleaseCheckStore


@pytest.fixture
def world(monkeypatch):
    store = ReleaseCheckStore(redis=lambda: None)
    events = []
    reads = []

    class _Logger:
        def log_activity(self, **kwargs):
            events.append(kwargs)

    async def fake_check(now=None):
        reads.append(now)
        state = {"last_attempt_at": 5.0, "last_success_at": 4.0, "error": "github: upstream returned 404", "failures": 1}
        store.write(state)
        return state

    patched = config.get_settings().model_copy(update={"release_check_enabled": True})
    monkeypatch.setattr(service, "get_settings", lambda: patched)
    monkeypatch.setattr(release_status, "get_store", lambda: store)
    monkeypatch.setattr(service, "check", fake_check)
    monkeypatch.setattr(release_status, "get_activity_logger", lambda: _Logger())
    return store, events, reads


async def test_a_second_check_within_a_minute_is_refused_with_retry_after(world) -> None:
    _store, events, reads = world
    first = await release_status.check_release_status_now(Response())
    assert first.error == "github: upstream returned 404"

    with pytest.raises(HTTPException) as refused:
        await release_status.check_release_status_now(Response())
    assert refused.value.status_code == 429
    assert 1 <= int(refused.value.headers["Retry-After"]) <= 60
    assert len(reads) == 1


async def test_the_check_is_audited_with_its_outcome(world) -> None:
    _store, events, _reads = world
    await release_status.check_release_status_now(Response())
    assert events[0]["action"] == "system.release_check.run"
    assert events[0]["result"] == "failure"
    assert events[0]["error_message"] == "github: upstream returned 404"


async def test_a_turned_off_check_refuses_to_run(world, monkeypatch) -> None:
    off = config.get_settings().model_copy(update={"release_check_enabled": False})
    monkeypatch.setattr(service, "get_settings", lambda: off)
    with pytest.raises(HTTPException) as refused:
        await release_status.check_release_status_now(Response())
    assert refused.value.status_code == 409


async def test_reading_the_status_never_reaches_the_feed(world) -> None:
    _store, _events, reads = world
    response = Response()
    status = await release_status.get_release_status(response)
    assert reads == [] and status.reasons == ["no_feed"]
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("group, expected", [("role_partner", 403), ("role_tenant", 403), ("role_admin", 200)])
def test_only_an_administrator_reaches_the_status(world, monkeypatch, group, expected) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.core import rbac
    from app.core.auth import AuthenticatedUser, get_current_user

    monkeypatch.setattr(rbac.activity_logger, "log_activity", lambda **_kwargs: None)
    app = FastAPI()
    app.include_router(release_status.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        user_id="u", email="someone@example.test", groups=[group],
    )
    client = TestClient(app)
    assert client.get("/api/v1/system/release-status").status_code == expected
    assert client.post("/api/v1/system/release-status/check").status_code in (
        (403,) if expected == 403 else (200,)
    )
