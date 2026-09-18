"""LXD refusing a group create reaches the caller with LXD's status and message, not a 500.

pylxd raises LXDAPIException on a 4xx. The route's generic handler turned that into a 500, so a
permission naming a resource in a project the group cannot view (LXD: "references project X,
but the project cannot be viewed by the group") read as a server fault in the dialog.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pylxd.exceptions import LXDAPIException

from app.core.dependencies import check_read_only_mode
from app.core.org_scope import org_scope_dependency
from app.core.rbac import get_current_user
from app.routers import permissions as permissions_module
from app.services.lxd_service import get_lxd_service
from tests._project_grant_fakes import admin


class _Response:
    def __init__(self, status_code: int, error: str) -> None:
        self.status_code = status_code
        self._error = error

    def json(self):
        return {"error": self._error, "error_code": self.status_code, "type": "error"}


class _Groups:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def post(self, json=None, timeout=None):
        raise self._exc


def _client(exc: Exception, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    fake_lxd = SimpleNamespace(api=SimpleNamespace(auth=SimpleNamespace(groups=_Groups(exc))))
    monkeypatch.setattr(permissions_module, "verify_cluster_access", lambda host: None)
    monkeypatch.setattr(permissions_module, "client_for", AsyncMock(return_value=fake_lxd))
    monkeypatch.setattr(permissions_module, "require_client", lambda client, host: client)
    app = FastAPI()
    app.include_router(permissions_module.groups_router, prefix="/api/v1")
    app.dependency_overrides[get_lxd_service] = lambda: SimpleNamespace()
    app.dependency_overrides[get_current_user] = lambda: admin()
    app.dependency_overrides[org_scope_dependency] = lambda: None
    app.dependency_overrides[check_read_only_mode] = lambda: None
    return TestClient(app)


def test_lxd_400_reaches_the_caller_as_400_with_lxd_message(monkeypatch: pytest.MonkeyPatch) -> None:
    message = 'Entitlement "can_view" on entity type "image" references project "p", but the project cannot be viewed by the group'
    client = _client(LXDAPIException(_Response(400, message)), monkeypatch)
    r = client.post("/api/v1/groups?host=nuc2", json={"name": "g", "description": "", "permissions": []})
    assert r.status_code == 400
    assert "cannot be viewed by the group" in r.json()["detail"]


def test_an_lxd_status_outside_4xx_is_a_502(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(LXDAPIException(_Response(500, "boom")), monkeypatch)
    r = client.post("/api/v1/groups?host=nuc2", json={"name": "g", "description": "", "permissions": []})
    assert r.status_code == 502


def test_any_other_failure_stays_a_500(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(RuntimeError("socket closed"), monkeypatch)
    r = client.post("/api/v1/groups?host=nuc2", json={"name": "g", "description": "", "permissions": []})
    assert r.status_code == 500
