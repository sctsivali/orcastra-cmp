"""A volume that is still attached must answer 409, and must not reach the daemon.

A 500 would describe the operator's own mistake as a server fault and hide the action it
calls for, detaching first. The 409 names the instances that hold the volume.

Unlike an image, there is no force here. LXD cannot remove an attached volume, so offering an
override would only move the same refusal one step later.

Hermetic, same shape as test_image_delete: no Vault, no pylxd, no database, no network.
"""

import pytest
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.lxd.lxd_service import LXDService


HOST = "nuc2"
POOL = "default"
VOLUME = "qa-fixture.iso"


class _FakeVolume:
    def __init__(self, used_by=None, on_delete=None):
        self.name = VOLUME
        self.type = "custom"
        self.used_by = list(used_by or [])
        self.project = "default"
        self.deleted = False
        self._on_delete = on_delete

    def delete(self):
        if self._on_delete:
            self._on_delete()
        self.deleted = True


class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class _FakeNode:
    """The raw API node the route addresses: storage-pools/<pool>/volumes/<type>/<name>.

    The daemon answers a name it cannot resolve with an opaque "not found" string, so the
    fake does the same and lets the router's error mapper decide the status.
    """

    def __init__(self, volume, path=()):
        self._volume = volume
        self._path = tuple(path)

    def __getitem__(self, segment):
        return _FakeNode(self._volume, self._path + (segment,))

    def _resolve(self):
        name = self._path[-1]
        if self._volume is None or name != self._volume.name:
            raise Exception(f"not found: {name}")
        return self._volume

    def get(self, params=None):
        volume = self._resolve()
        return _FakeResponse({"metadata": {"used_by": list(volume.used_by)}})

    def delete(self, params=None):
        self._resolve().delete()


class _FakeClient:
    def __init__(self, volume):
        self.api = _FakeNode(volume)


class _ConnectionManager:
    def __init__(self, client):
        self._client = client

    def get_client(self, host, *args, **kwargs):
        return self._client if host == HOST else None

    def get_node_names(self):
        return [HOST]


def _service(volume) -> LXDService:
    svc = LXDService.__new__(LXDService)
    svc.connection_manager = _ConnectionManager(_FakeClient(volume))
    svc.cert_manager = None
    return svc


@pytest.fixture(autouse=True)
def _no_webhook(monkeypatch):
    """Stub the OrcaHub webhook, event emitter and configuration lock (they need a DB)."""
    from app.routers import storage_volumes
    from app.services.webhooks import emitter

    monkeypatch.setattr(storage_volumes, "schedule_orcahub_event", lambda *a, **k: None)

    async def no_event(**_kwargs):
        return None

    monkeypatch.setattr(emitter, "emit_resource_event", no_event)

    @asynccontextmanager
    async def no_configuration_lock(_db, _host):
        yield

    monkeypatch.setattr(
        storage_volumes,
        "serialize_cluster_configuration",
        no_configuration_lock,
    )


def _client(svc) -> TestClient:
    from app.core.org_scope import org_scope_dependency
    from app.core.rbac import get_current_user
    from app.routers import storage_volumes
    from app.services.lxd_service import get_lxd_service

    app = FastAPI()
    app.include_router(storage_volumes.router)
    app.dependency_overrides[get_lxd_service] = lambda: svc
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def _delete(svc):
    return _client(svc).request(
        "DELETE",
        f"/storage/volumes/custom/{VOLUME}",
        params={"host": HOST, "pool": POOL},
    )


def test_an_unused_volume_is_deleted():
    volume = _FakeVolume()
    r = _delete(_service(volume))
    assert r.status_code == 200, r.text
    assert volume.deleted is True


def test_an_attached_volume_is_409_not_500():
    volume = _FakeVolume(used_by=["/1.0/instances/web-01?project=acme"])
    r = _delete(_service(volume))
    assert r.status_code == 409, r.text
    assert volume.deleted is False, "a refused delete must not have reached the daemon"


def test_the_409_names_the_instance_not_the_api_path():
    """used_by holds API paths; the operator needs the name they know the instance by."""
    volume = _FakeVolume(
        used_by=[
            "/1.0/instances/web-01?project=acme",
            "/1.0/instances/db-02?project=acme",
        ]
    )
    detail = _delete(_service(volume)).json()["detail"]
    assert "web-01" in detail and "db-02" in detail, detail
    assert "/1.0/instances" not in detail, detail


def test_a_daemon_refusal_after_the_precheck_is_still_409():
    """The pre-check reads used_by from a list the daemon built a moment earlier, so the
    daemon still owns the race. Losing it still answers 409."""

    def _boom():
        raise Exception("Failed to delete storage volume: volume is in use")

    volume = _FakeVolume(on_delete=_boom)
    r = _delete(_service(volume))
    assert r.status_code == 409, r.text


def test_a_missing_volume_is_404_not_500():
    r = _client(_service(None)).request(
        "DELETE", "/storage/volumes/custom/ghost", params={"host": HOST, "pool": POOL}
    )
    assert r.status_code == 404, r.text


def test_an_unreachable_cluster_is_503():
    svc = _service(_FakeVolume())
    svc.connection_manager._client = None
    r = _delete(svc)
    assert r.status_code == 503, r.text
