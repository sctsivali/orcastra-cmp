"""A storage pool the daemon refuses to change reads as the refusal it is.

A refusal such as LXD's "currently in use" must not become a 500: main.py hides a 500's body,
which leaves the operator nothing to act on. The routes answer through `lxd_http_error`, as
network and image deletes do.
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pylxd.exceptions import LXDAPIException

from app.core.dependencies import check_read_only_mode
from app.core.org_scope import org_scope_dependency
from app.core.rbac import get_current_user
from app.routers import storage_pools as pools_module
from app.services.lxd_service import get_lxd_service


@asynccontextmanager
async def _no_configuration_lock(_db, _host):
    yield


def _refusal(status: int, text: str) -> LXDAPIException:
    return LXDAPIException(SimpleNamespace(status_code=status, json=lambda: {"error": text}))


class _Pool:
    config: dict = {}
    description = ""

    def __init__(self, failure: Exception) -> None:
        self._failure = failure

    def delete(self, wait: bool = False) -> None:
        raise self._failure

    def save(self, wait: bool = False) -> None:
        raise self._failure


def _client(monkeypatch, failure: Exception) -> TestClient:
    def create(_body: Any) -> None:
        raise failure

    pools = SimpleNamespace(get=lambda _name: _Pool(failure), create=create)
    client = SimpleNamespace(storage_pools=pools)
    svc = SimpleNamespace(get_client_with_reconnect=lambda _host: client)
    monkeypatch.setattr(pools_module, "is_clustered", lambda _c: False)
    monkeypatch.setattr(pools_module, "verify_cluster_access", lambda _host: None)
    monkeypatch.setattr(pools_module, "serialize_cluster_configuration", _no_configuration_lock)
    monkeypatch.setattr(pools_module.activity_logger, "log_activity", lambda **_kw: None)
    app = FastAPI()
    app.include_router(pools_module.router)
    app.dependency_overrides[get_lxd_service] = lambda: svc
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    app.dependency_overrides[check_read_only_mode] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def test_a_pool_still_in_use_is_a_conflict_that_says_so(monkeypatch) -> None:
    client = _client(monkeypatch, _refusal(400, "The storage pool is currently in use"))
    response = client.delete("/storage/pools/qa-dir", params={"host": "lab"})
    assert response.status_code == 409, response.text
    assert "in use" in response.json()["detail"]


def test_a_pool_name_that_is_taken_is_a_conflict(monkeypatch) -> None:
    client = _client(monkeypatch, _refusal(400, "Storage pool already exists"))
    response = client.post(
        "/storage/pools",
        params={"host": "lab"},
        json={"name": "qa-dir", "driver": "dir", "description": "", "config": {}},
    )
    assert response.status_code == 409, response.text


def test_a_config_the_daemon_refuses_is_the_callers_to_fix(monkeypatch) -> None:
    client = _client(monkeypatch, _refusal(400, 'Invalid value for option "rsync.bwlimit"'))
    response = client.put(
        "/storage/pools/qa-dir",
        params={"host": "lab"},
        json={"config": {"rsync.bwlimit": "lots"}},
    )
    assert response.status_code == 400, response.text
    assert "rsync.bwlimit" in response.json()["detail"]


@pytest.mark.parametrize("failure", [RuntimeError("boom"), ValueError("bad state")])
def test_a_fault_that_is_not_a_refusal_stays_a_server_fault(monkeypatch, failure: Exception) -> None:
    client = _client(monkeypatch, failure)
    response = client.delete("/storage/pools/qa-dir", params={"host": "lab"})
    assert response.status_code == 500, response.text
