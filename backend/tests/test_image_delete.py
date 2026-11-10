"""Deleting an image must refuse by default when instances were built from it.

LXD permits the delete and the instances keep running, but nothing can launch or refresh from
that fingerprint again; for a custom image whose source artifact is gone the loss is permanent.
The endpoint refuses with 409 naming the dependents, and `force=true` overrides. The refusal
lives on the server: the dashboard's dependent count comes from a tenant-filtered list and can
undercount, and `/integrations/raw-proxy` forwards DELETE /1.0/images/{fp} for a cluster-admin
key with no dialog at all.

Hermetic: `LXDService.__new__` skips `__init__` and the connection manager hands back a fake
client.
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
    """One image record as the daemon answers it, plus the delete the test observes."""

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
        self.seen_params = []

    def delete(self, wait=False):
        self.deleted = True

    def row(self):
        return {
            "fingerprint": self.fingerprint, "aliases": self.aliases,
            "properties": self.properties, "architecture": self.architecture,
            "size": self.size, "uploaded_at": self.uploaded_at, "public": self.public,
            "type": self.type, "cached": self.cached, "auto_update": self.auto_update,
        }


class _FakeResponse:
    def __init__(self, metadata):
        self._metadata = metadata

    def json(self):
        return {"metadata": self._metadata}


class _FakeImageNode:
    """`client.api.images[<fp>]`: the raw API node the service reads and deletes through."""

    def __init__(self, image, fingerprint):
        self._image = image
        self._fingerprint = fingerprint

    def _match(self):
        return self._image is not None and self._fingerprint == self._image.fingerprint

    def get(self, params=None, timeout=None):
        if not self._match():
            raise Exception(f"Image not found: {self._fingerprint}")
        self._image.seen_params.append(dict(params or {}))
        return _FakeResponse(self._image.row())

    def delete(self, params=None, timeout=None):
        if not self._match():
            raise Exception(f"Image not found: {self._fingerprint}")
        self._image.seen_params.append(dict(params or {}))
        self._image.delete(wait=True)
        # A synchronous answer: no operation to wait on.
        return _FakeResponse(None)


class _FakeImagesApi:
    def __init__(self, image):
        self._image = image

    def __getitem__(self, fingerprint):
        return _FakeImageNode(self._image, fingerprint)


class _FakeProjectsApi:
    """`client.api.projects.get(recursion=1)`: the project configs the owner rule reads."""

    def __init__(self, projects):
        self._projects = projects

    def get(self, params=None, timeout=None):
        return _FakeResponse(
            [{"name": name, "config": config} for name, config in self._projects.items()]
        )


class _FakeApi:
    def __init__(self, image, projects):
        self.images = _FakeImagesApi(image)
        self.projects = _FakeProjectsApi(projects)


class _FakeClient:
    def __init__(self, image, projects=None):
        self.api = _FakeApi(image, projects or {"default": {}})
        self.host_info = {"api_extensions": ["images_all_projects"]}


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


def _service(image=None, vms=None, projects=None) -> LXDService:
    svc = LXDService.__new__(LXDService)
    conn = _ConnectionManager(_FakeClient(image, projects) if image is not None else None)
    svc.connection_manager = conn
    svc.cert_manager = None
    svc.image_manager = ImageManager(conn, None)

    class _InstanceManager:
        # The dependents read asks the one host for its instances across every project.
        def fetch_node_instances(self, host, endpoint=""):
            if host != HOST:
                return [], False
            return [v for v in (vms or []) if getattr(v, "host", HOST) == host], True

    svc.instance_manager = _InstanceManager()
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
    """The real router and dependencies, with only auth and org scope stubbed.

    This exercises the real except-clause ordering: a 403, 404 or 409 raised inside the
    handler's try must not be re-raised as a 500 by the broad handler below it.
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
    # The detail names the dependents so the operator does not have to look them up.
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
    """volatile.base_image is the usual key, image.fingerprint the fallback.

    The tenant image filter reads both, so this path does too: otherwise an image visible to a
    tenant through one key could be deleted without warning through the other.
    """
    image = _FakeImage()
    svc = _service(image, vms=[_VM("legacy-01", key="image.fingerprint")])

    r = _client(svc).delete(f"/images/{FP}", params={"host": HOST})

    assert r.status_code == 409, r.text
    assert "legacy-01" in r.json()["detail"]


def test_an_instance_on_another_host_is_not_a_dependency():
    """Fingerprints are per-cluster. A row is (host, fingerprint), and so is the scan."""
    image = _FakeImage()
    svc = _service(image, vms=[_VM("elsewhere-01", host="center2")])

    r = _client(svc).delete(f"/images/{FP}", params={"host": HOST})

    assert r.status_code == 200, r.text


def test_an_unknown_fingerprint_is_404_not_500():
    svc = _service(_FakeImage(fingerprint=OTHER_FP))
    r = _client(svc).delete(f"/images/{FP}", params={"host": HOST})
    assert r.status_code == 404, r.text


def test_a_short_fingerprint_is_refused():
    """LXD resolves a prefix, so a short string could delete whichever image matched it first."""
    r = _client(_service(_FakeImage())).delete("/images/abc123", params={"host": HOST})
    assert r.status_code == 422, r.text


def test_an_uppercase_fingerprint_is_refused():
    r = _client(_service(_FakeImage())).delete(f"/images/{'A' * 64}", params={"host": HOST})
    assert r.status_code == 422, r.text


def test_a_missing_host_is_refused():
    r = _client(_service(_FakeImage())).delete(f"/images/{FP}")
    assert r.status_code == 422, r.text


def test_an_unreachable_cluster_is_503_not_500():
    """The breaker returns None without a network call while a cluster is down; that is a 503."""
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
    # The flag tells a routine cleanup apart from one that overrode a refusal.
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
    """The dialog reads this before offering the delete, so the dependents are shown first."""
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
    """GET /images/{host} takes a fingerprint-shaped path parameter too.

    The assertion is about which handler each path reaches: this fake has no cert manager, so
    the list handler cannot succeed here.
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


def test_a_delete_names_the_store_the_project_reads():
    """The daemon is asked about the project's own store if it keeps images, else default."""
    own = _FakeImage()
    svc = _service(own, projects={"default": {}, "dev01": {"features.images": "true"}})
    r = _client(svc).delete(f"/images/{FP}", params={"host": HOST, "project": "dev01"})
    assert r.status_code == 200, r.text
    assert own.deleted is True
    assert all(p.get("project") == "dev01" for p in own.seen_params), own.seen_params
    assert "dev01" in r.json()["message"]

    shared = _FakeImage()
    svc = _service(shared, projects={"default": {}, "qa-cap": {"features.images": "false"}})
    r = _client(svc).delete(f"/images/{FP}", params={"host": HOST, "project": "qa-cap"})
    assert r.status_code == 200, r.text
    assert all(p.get("project") == "default" for p in shared.seen_params), shared.seen_params


def test_an_invalid_project_name_is_refused_before_the_daemon_is_asked():
    image = _FakeImage()
    r = _client(_service(image)).delete(f"/images/{FP}", params={"host": HOST, "project": "../x"})
    assert r.status_code == 422
    assert image.deleted is False


def test_a_missing_image_names_the_store_it_was_looked_for_in():
    svc = _service(_FakeImage(fingerprint=OTHER_FP), projects={"default": {}, "dev01": {"features.images": "true"}})
    r = _client(svc).delete(f"/images/{FP}", params={"host": HOST, "project": "dev01"})
    assert r.status_code == 404
    assert "dev01" in r.json()["detail"]


def test_a_dependent_in_another_project_on_the_host_refuses_the_delete():
    # An own-store project's instance is built from the store's image; the host-wide read
    # sees it whatever project it sits in, so the delete is refused without force.
    image = _FakeImage()
    vm = _VM("built-elsewhere")
    vm.project = "qa-store"
    svc = _service(image, vms=[vm])

    r = _client(svc).delete(f"/images/{FP}", params={"host": HOST, "project": "qa-store"})

    assert r.status_code == 409, r.text
    assert "built-elsewhere" in r.json()["detail"]


def test_an_unreadable_host_never_lets_a_delete_through():
    # The host's instances could not be read, so the dependents are unknown and the delete is
    # refused.
    image = _FakeImage()
    svc = _service(image, vms=[])

    class _Down:
        def fetch_node_instances(self, host, endpoint=""):
            return [], False

    svc.instance_manager = _Down()

    r = _client(svc).delete(f"/images/{FP}", params={"host": HOST})

    assert r.status_code == 503, r.text
    assert image.deleted is False


def _bounded(client_call):
    """Run one request as a Partner whose bound on HOST is the default project only."""
    import contextvars

    from app.core import org_scope

    def run():
        org_scope._visible_projects_var.set({HOST: {"default"}})
        org_scope._allowed_clusters_var.set(None)
        return client_call()

    return contextvars.copy_context().run(run)


def test_a_bounded_partner_may_delete_from_the_default_store_a_shared_project_reads():
    # The shared project is outside the bound but keeps no store: the image sits in the default
    # store, which the bound allows, so the delete is judged on that store and goes through.
    image = _FakeImage()
    svc = _service(image, projects={"default": {}, "qa-shared": {"features.images": "false"}})
    client = _client(svc)

    r = _bounded(lambda: client.delete(f"/images/{FP}", params={"host": HOST, "project": "qa-shared"}))

    assert r.status_code == 200, r.text
    assert image.deleted is True


def test_a_bounded_partner_is_refused_an_own_store_outside_the_bound():
    image = _FakeImage()
    image.project = "cust-other"
    svc = _service(image, projects={"default": {}, "cust-other": {"features.images": "true"}})
    client = _client(svc)

    r = _bounded(lambda: client.delete(f"/images/{FP}", params={"host": HOST, "project": "cust-other"}))

    assert r.status_code == 403, r.text
    assert image.deleted is False


def test_dependents_outside_the_bound_are_counted_but_not_named():
    # The refusal still stands on a dependent the caller cannot see; the detail counts it
    # and names only what the bound allows.
    image = _FakeImage()
    seen = _VM("mine-01")
    hidden = _VM("theirs-01")
    hidden.project = "cust-other"
    # The other project shares the default store, so its instance is this store's dependent.
    svc = _service(image, vms=[seen, hidden], projects={"default": {}, "cust-other": {"features.images": "false"}})
    client = _client(svc)

    usage = _bounded(lambda: client.get(f"/images/{FP}/usage", params={"host": HOST}))
    assert usage.status_code == 200, usage.text
    assert usage.json()["dependents"] == ["mine-01"]
    assert usage.json()["hidden_dependents"] == 1

    r = _bounded(lambda: client.delete(f"/images/{FP}", params={"host": HOST}))
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "2 instance(s)" in detail and "mine-01" in detail and "theirs-01" not in detail
