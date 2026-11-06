"""POST /storage/volumes creates the volume in the project it names.

The route took no project, built its call on the pooled client (which carries none) and so put
every volume in `default`, whatever project the caller worked in (RC5 acceptance F-02,
2026-11-05). It now takes `project` and `location` and is gated exactly like update and delete:
the project must be visible, a project without its own volumes resolves to `default` as LXD
resolves it, and `default` stays view only where another party holds the cluster.
"""

from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

HOST = "lab"


class _Response:
    def __init__(self, body: Dict[str, Any]) -> None:
        self._body = body

    def json(self) -> Dict[str, Any]:
        return self._body


class _Node:
    def __init__(self, client: "_Client", path: str) -> None:
        self.client, self.path = client, path

    def __getitem__(self, segment: str) -> "_Node":
        return _Node(self.client, f"{self.path}/{segment}")

    def __getattr__(self, segment: str) -> "_Node":
        return self[segment]

    def post(self, json: Dict[str, Any], params: Optional[Dict[str, str]] = None, **_: Any) -> _Response:
        self.client.calls.append(("POST", self.path, json, params))
        return _Response({"type": "sync", "metadata": {}})

    def get(self, params: Optional[Dict[str, Any]] = None, **_: Any) -> _Response:
        self.client.calls.append(("GET", self.path, None, params))
        if self.path == "/projects":
            return _Response({"metadata": [
                {"name": "default", "config": {}},
                {"name": "acme", "config": {"features.storage.volumes": "true"}},
                {"name": "shared-vols", "config": {"features.storage.volumes": "false"}},
            ]})
        name = self.path.rsplit("/", 1)[-1]
        return _Response({"metadata": {"name": name, "type": "custom", "config": {"size": "1GiB"},
                                       "content_type": "filesystem", "location": "member-2"}})


class _Client:
    def __init__(self) -> None:
        self.calls: List[tuple] = []
        self.api = _Node(self, "")


@pytest.fixture
def lab(monkeypatch):
    from app.routers import storage_volumes

    client = _Client()

    @asynccontextmanager
    async def unlocked(_db, _host):
        yield

    async def _client_for(_service, _host):
        return client

    async def _no_fence(**_kwargs):
        return None

    monkeypatch.setattr(storage_volumes, "serialize_cluster_configuration", unlocked)
    monkeypatch.setattr(storage_volumes, "client_for", _client_for)
    monkeypatch.setattr(storage_volumes, "assert_project_not_migration_fenced", _no_fence)
    monkeypatch.setattr(storage_volumes, "schedule_orcahub_event", lambda *a, **k: None)

    async def _no_event(**_kwargs):
        return None

    import app.services.webhooks.emitter as emitter

    monkeypatch.setattr(emitter, "emit_resource_event", _no_event)
    return client


def _app() -> TestClient:
    from app.core.database import get_db
    from app.core.dependencies import check_read_only_mode
    from app.core.org_scope import org_scope_dependency
    from app.core.rbac import get_current_user
    from app.routers import storage_volumes
    from app.services.lxd_service import get_lxd_service

    app = FastAPI()
    app.include_router(storage_volumes.router)
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    app.dependency_overrides[check_read_only_mode] = lambda: None
    app.dependency_overrides[get_lxd_service] = lambda: None
    app.dependency_overrides[get_db] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def _create(query: str):
    return _app().post(f"/storage/volumes?host={HOST}{query}", json={"name": "data-01", "pool": "local"})


def _posts(client: _Client):
    return [(path, params) for method, path, _body, params in client.calls if method == "POST"]


def test_a_named_project_with_its_own_volumes_holds_the_new_volume(lab) -> None:
    response = _create("&project=acme&location=member-2")
    assert response.status_code == 200, response.text
    assert _posts(lab) == [("/storage-pools/local/volumes/custom", {"project": "acme", "target": "member-2"})]
    volume = response.json()["volume"]
    assert volume["project"] == "acme" and volume["location"] == "member-2"


def test_a_project_without_its_own_volumes_creates_in_default_as_lxd_does(lab) -> None:
    assert _create("&project=shared-vols").status_code == 200
    assert _posts(lab) == [("/storage-pools/local/volumes/custom", None)]
    assert _create("").json()["volume"]["project"] == "default"


def test_a_project_out_of_scope_is_refused_before_the_cluster_is_asked(lab, monkeypatch) -> None:
    from app.routers import storage_volumes

    monkeypatch.setattr(storage_volumes, "project_is_visible", lambda _host, project: project != "acme")
    assert _create("&project=acme").status_code == 403
    assert lab.calls == []


def test_a_view_only_default_still_refuses_the_create(lab, monkeypatch) -> None:
    from app.routers import storage_volumes

    def refuse(_host, project):
        if project == "default":
            raise HTTPException(status_code=403, detail="view only")

    monkeypatch.setattr(storage_volumes, "refuse_default_operation", refuse)
    assert _create("&project=shared-vols").status_code == 403
    assert _posts(lab) == []
    assert _create("&project=acme").status_code == 200


@pytest.mark.parametrize("bad", ["default?x=1", "a/b", "../x"])
def test_a_hostile_project_or_location_is_refused_at_the_boundary(lab, bad) -> None:
    assert _create(f"&project={bad}").status_code == 422
    assert _create(f"&location={bad}").status_code == 422
    assert lab.calls == []
