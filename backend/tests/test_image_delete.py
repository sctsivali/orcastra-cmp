"""Deleting an image must refuse by default when instances were built from it.

LXD permits the delete. The instances keep running, because their root disk no longer needs
the image record. What is lost is the ability to launch or refresh from that fingerprint, and
for a custom image whose source artifact is gone, that loss is permanent.

So the endpoint refuses with 409 and names the dependents, and `force=true` overrides. The
refusal lives on the server rather than in the confirm dialog for two reasons. The dashboard's
own dependent count comes from a tenant-filtered instance list, so it can be an undercount.
And `/integrations/raw-proxy` already forwards DELETE /1.0/images/{fp} for an API key holding
cluster-admin, which has no dialog at all.

Hermetic, following test_unreachable_cluster_is_503: `LXDService.__new__` skips `__init__`, so
there is no Vault client, no pylxd and no cluster sync. The connection manager hands back a
fake client. No database, no Redis, no network.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.lxd.images import ImageManager
from app.services.lxd.lxd_service import LXDService


HOST = "nuc2"
FP = "a" * 64
OTHER_FP = "b" * 64


class _FakeImage:
    def __init__(self, fingerprint=FP, cached=True, public=False):
        self.fingerprint = fingerprint
        self.aliases = [{"name": "qa/base"}]
        self.properties = {"description": "QA base image"}
        self.architecture = "x86_64"
        self.size = 1234
        self.uploaded_at = None
        self.public = public
        self.type = "container"
        self.cached = cached
        self.auto_update = False
        self.deleted = False

    def delete(self, wait=False):
        self.deleted = True


class _FakeImages:
    def __init__(self, image):
        self._image = image

    def get(self, fingerprint):
        if self._image is None or fingerprint != self._image.fingerprint:
            raise Exception(f"Image not found: {fingerprint}")
        return self._image


class _FakeClient:
    def __init__(self, image):
        self.images = _FakeImages(image)


class _ConnectionManager:
    def __init__(self, client):
        self._client = client

    def get_client(self, host, *args, **kwargs):
        return self._client if host == HOST else None

    def get_node_names(self):
        return [HOST]


class _VM:
    def __init__(self, name, host=HOST, fingerprint=FP, key="volatile.base_image"):
        self.name = name
        self.host = host
        self.project = "default"
        self.config = {key: fingerprint} if fingerprint else {}


def _service(image=None, vms=None) -> LXDService:
    svc = LXDService.__new__(LXDService)
    conn = _ConnectionManager(_FakeClient(image) if image is not None else None)
    svc.connection_manager = conn
    svc.cert_manager = None
    svc.image_manager = ImageManager(conn, None)

    async def _list_all_vms():
        return list(vms or []), [HOST]

    svc.list_all_vms = _list_all_vms
    return svc


class _RecordingAudit:
    def __init__(self):
        self.entries = []

    def log_activity(self, **kwargs):
        self.entries.append(kwargs)


@pytest.fixture
def audit(monkeypatch):
    from app.routers import images

    recorder = _RecordingAudit()
    monkeypatch.setattr(images, "activity_logger", recorder)
    return recorder


def _client(svc) -> TestClient:
    """One real router, real dependencies, only auth and org scope stubbed.

    The point is to exercise the real except-clause ordering: a 403, 404 or 409 raised inside
    the handler's try must not be re-raised as a 500 by the broad handler below it.
    """
    from app.core.org_scope import org_scope_dependency
    from app.core.rbac import get_current_user
    from app.routers import images
    from app.services.lxd_service import get_lxd_service

    app = FastAPI()
    app.include_router(images.router)
    app.dependency_overrides[get_lxd_service] = lambda: svc
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def test_an_unused_image_is_deleted():
    image = _FakeImage()
    r = _client(_service(image)).delete(f"/images/{FP}", params={"host": HOST})
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True
    assert r.json()["forced"] is False
    assert image.deleted is True


def test_a_dependent_instance_refuses_the_delete():
    image = _FakeImage()
    svc = _service(image, vms=[_VM("web-01"), _VM("web-02")])

    r = _client(svc).delete(f"/images/{FP}", params={"host": HOST})

    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    # Naming them is the whole point: "in use" without saying by what leaves the operator to
    # go and find out, which is the step they will skip.
    assert "web-01" in detail and "web-02" in detail, detail
    assert image.deleted is False, "a refused delete must not have reached the daemon"


def test_force_overrides_a_dependent_instance():
    image = _FakeImage()
    svc = _service(image, vms=[_VM("web-01")])

    r = _client(svc).delete(f"/images/{FP}", params={"host": HOST, "force": "true"})

    assert r.status_code == 200, r.text
    assert r.json()["forced"] is True
    assert image.deleted is True


def test_the_second_config_key_counts_as_a_dependency():
    """volatile.base_image is the usual key, image.fingerprint is the fallback.

    The tenant image filter reads both, in that order. If this path read only the first, an
    image visible to a tenant because of one key could be deleted without warning because of
    the other.
    """
    image = _FakeImage()
    svc = _service(image, vms=[_VM("legacy-01", key="image.fingerprint")])

    r = _client(svc).delete(f"/images/{FP}", params={"host": HOST})

    assert r.status_code == 409, r.text
    assert "legacy-01" in r.json()["detail"]


def test_an_instance_on_another_host_is_not_a_dependency():
    """Fingerprints are per-cluster. A row is (host, fingerprint), and so is the scan."""
    image = _FakeImage()
    svc = _service(image, vms=[_VM("elsewhere-01", host="central2")])

    r = _client(svc).delete(f"/images/{FP}", params={"host": HOST})

    assert r.status_code == 200, r.text


def test_an_unknown_fingerprint_is_404_not_500():
    svc = _service(_FakeImage(fingerprint=OTHER_FP))
    r = _client(svc).delete(f"/images/{FP}", params={"host": HOST})
    assert r.status_code == 404, r.text


def test_a_short_fingerprint_is_refused():
    """LXD resolves a prefix. Accepting one would let a short string delete whichever image
    happened to match it first, which is the wrong image with no way to tell."""
    r = _client(_service(_FakeImage())).delete("/images/abc123", params={"host": HOST})
    assert r.status_code == 422, r.text


def test_an_uppercase_fingerprint_is_refused():
    r = _client(_service(_FakeImage())).delete(f"/images/{'A' * 64}", params={"host": HOST})
    assert r.status_code == 422, r.text


def test_a_missing_host_is_refused():
    r = _client(_service(_FakeImage())).delete(f"/images/{FP}")
    assert r.status_code == 422, r.text


def test_an_unreachable_cluster_is_503_not_500():
    """The breaker returns None without a network call, which is the steady state for a
    cluster that is down. 500 pages an engineer for an outage they already know about."""
    svc = _service(_FakeImage())
    svc.connection_manager._client = None

    r = _client(svc).delete(f"/images/{FP}", params={"host": HOST})

    assert r.status_code == 503, r.text


def test_success_is_audited_with_the_force_flag(audit):
    image = _FakeImage(cached=False, public=True)
    svc = _service(image, vms=[_VM("web-01")])

    _client(svc).delete(f"/images/{FP}", params={"host": HOST, "force": "true"})

    assert len(audit.entries) == 1
    entry = audit.entries[0]
    assert entry["action"] == "image.delete"
    assert entry["result"] == "success"
    assert entry["target_id"] == FP
    assert entry["target_host"] == HOST
    # An override is the entry worth having. Without the flag the audit trail cannot tell a
    # routine cleanup apart from one that overrode a refusal.
    assert entry["details"]["force"] is True
    assert entry["details"]["dependents"] == 1
    assert entry["details"]["cached"] is False
    assert entry["details"]["public"] is True


def test_a_daemon_failure_is_audited_as_a_failure(audit):
    image = _FakeImage()

    def _boom(wait=False):
        raise Exception("Failed to delete image: still in use by something")

    image.delete = _boom
    r = _client(_service(image)).delete(f"/images/{FP}", params={"host": HOST})

    assert r.status_code == 409, r.text
    assert len(audit.entries) == 1
    assert audit.entries[0]["result"] == "failure"


def test_a_refusal_is_not_audited_as_a_delete(audit):
    """A 409 changed nothing, so recording it as an image.delete would put a deletion that
    never happened into the high-severity audit stream."""
    svc = _service(_FakeImage(), vms=[_VM("web-01")])

    _client(svc).delete(f"/images/{FP}", params={"host": HOST})

    assert audit.entries == []


# --- The preflight read the confirm dialog uses --------------------------------------------

def test_usage_names_the_dependent_instances():
    """The dialog reads this before offering the delete, so the operator sees the truth before
    committing rather than after being refused."""
    svc = _service(_FakeImage(), vms=[_VM("web-01"), _VM("web-02"), _VM("other", fingerprint=OTHER_FP)])

    r = _client(svc).get(f"/images/{FP}/usage", params={"host": HOST})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dependents"] == ["web-01", "web-02"]
    assert body["host"] == HOST
    assert body["fingerprint"] == FP


def test_usage_is_empty_for_an_unused_image():
    r = _client(_service(_FakeImage())).get(f"/images/{FP}/usage", params={"host": HOST})
    assert r.status_code == 200, r.text
    assert r.json()["dependents"] == []


def test_usage_does_not_collide_with_the_per_host_list_route():
    """GET /images/{host} was already registered and its path parameter is a fingerprint-shaped
    string too. The assertion is about which handler each path reaches, not about what the
    list handler answers: this fake has no cert manager, so the list cannot succeed here.
    """
    svc = _service(_FakeImage(), vms=[_VM("web-01")])
    client = _client(svc)

    # Two segments must reach image_usage.
    usage = client.get(f"/images/{FP}/usage", params={"host": HOST})
    assert usage.status_code == 200, usage.text
    assert usage.json()["dependents"] == ["web-01"]

    # One segment must not. Whatever list_node_images answers, it is not a usage payload.
    listing = client.get(f"/images/{HOST}")
    assert "dependents" not in listing.text, listing.text


def test_usage_refuses_a_short_fingerprint():
    r = _client(_service(_FakeImage())).get("/images/abc123/usage", params={"host": HOST})
    assert r.status_code == 422, r.text
