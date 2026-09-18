"""An image read names the store that holds each image, and a project reads only its own.

LXD resolves an instance's image inside the target project alone, so a project that isolates
its images (`features.images=true`) cannot create from a default-store fingerprint. The
catalogue therefore has to read the store the project really uses, and the Images page has
to say which store a row came from. Same module as networks, profiles and volumes.
"""
import asyncio

import pytest

from app.services.lxd.images import ImageManager, ImageStoreListing, _info_from_row
from app.services.lxd.project_scope import DEFAULT_PROJECT, effective_project

from test_a_project_read_names_the_project_that_holds_it import FakeClient

FP_A = "a" * 64
FP_B = "b" * 64


def _row(fingerprint, project=None, **extra):
    row = {
        "fingerprint": fingerprint,
        "aliases": [{"name": "ubuntu/24.04", "description": ""}],
        "properties": {"description": "Ubuntu noble", "os": "Ubuntu"},
        "architecture": "x86_64",
        "size": 300_000_000,
        "uploaded_at": "2026-09-01T00:00:00Z",
        "public": False,
        "type": "container",
        "cached": True,
        "auto_update": False,
        "created_at": "0001-01-01T00:00:00Z",
        "profiles": ["default"],
    }
    if project:
        row["project"] = project
    row.update(extra)
    return row


class _Conn:
    def __init__(self, client):
        self._client = client

    def get_client(self, host, *args, **kwargs):
        return self._client


def _manager(client) -> ImageManager:
    return ImageManager(_Conn(client), None)


@pytest.mark.parametrize(
    "config,expected",
    [
        ({"features.images": "true"}, "dev01"),
        ({"features.images": "false"}, DEFAULT_PROJECT),
        ({}, DEFAULT_PROJECT),
    ],
)
def test_the_owner_of_an_image_store_follows_features_images(config, expected):
    assert effective_project("images", "dev01", {"dev01": config}) == expected


def test_the_row_builder_maps_the_daemon_record():
    info = _info_from_row(_row(FP_A, project="dev01"))
    assert info.fingerprint == FP_A
    assert info.aliases == ["ubuntu/24.04"]
    assert info.description == "Ubuntu noble"
    assert info.project == "dev01"
    assert info.created_at is None, "LXD's zero time is not a timestamp"
    assert info.upload_date is not None
    assert info.profiles == ["default"]


def test_the_row_builder_is_defensive_about_a_sparse_record():
    info = _info_from_row({"fingerprint": FP_B, "architecture": "x86_64", "size": 1})
    assert info.auto_update is False
    assert info.cached is False
    assert info.project == DEFAULT_PROJECT
    assert info.type == "container"


def test_a_project_with_its_own_store_reads_only_that_store():
    client = FakeClient(
        extensions=("images_all_projects",),
        projects={DEFAULT_PROJECT: {}, "dev01": {"features.images": "true"}},
        per_project={DEFAULT_PROJECT: [_row(FP_A)], "dev01": [_row(FP_B)]},
    )
    listing = asyncio.run(_manager(client).list_images("nuc2", "dev01"))
    assert isinstance(listing, ImageStoreListing)
    assert listing.project == "dev01"
    assert [i.fingerprint for i in listing.images] == [FP_B]
    assert listing.images[0].project == "dev01"
    assert ("images", {"recursion": 1, "project": "dev01"}) in client.calls


def test_a_project_sharing_the_default_store_reads_default_and_says_so():
    client = FakeClient(
        extensions=("images_all_projects",),
        projects={DEFAULT_PROJECT: {}, "qa-cap": {"features.images": "false"}},
        per_project={DEFAULT_PROJECT: [_row(FP_A)]},
    )
    listing = asyncio.run(_manager(client).list_images("nuc2", "qa-cap"))
    assert listing.project == DEFAULT_PROJECT
    assert [i.project for i in listing.images] == [DEFAULT_PROJECT]
    assert ("images", {"recursion": 1, "project": DEFAULT_PROJECT}) in client.calls


def test_the_default_project_never_reads_the_project_list():
    client = FakeClient(extensions=("images_all_projects",), per_project={DEFAULT_PROJECT: [_row(FP_A)]})
    asyncio.run(_manager(client).list_images("nuc2", DEFAULT_PROJECT))
    assert all(collection != "projects" for collection, _ in client.calls)


def test_no_project_reads_every_store_in_one_request_when_the_daemon_can():
    client = FakeClient(
        extensions=("images_all_projects",),
        all_projects_rows=[_row(FP_A, project=DEFAULT_PROJECT), _row(FP_B, project="dev01")],
    )
    listing = asyncio.run(_manager(client).list_images("nuc2"))
    assert listing.project is None and listing.complete is True
    assert sorted(i.project for i in listing.images) == [DEFAULT_PROJECT, "dev01"]
    assert client.calls == [("images", {"recursion": 1, "all-projects": "true"})]


def test_no_project_walks_the_owners_when_the_daemon_cannot():
    client = FakeClient(
        extensions=(),
        projects={
            DEFAULT_PROJECT: {},
            "dev01": {"features.images": "true"},
            "qa-cap": {"features.images": "false"},
        },
        per_project={DEFAULT_PROJECT: [_row(FP_A)], "dev01": [_row(FP_B)]},
    )
    listing = asyncio.run(_manager(client).list_images("nuc2"))
    assert listing.complete is True
    assert sorted(i.project for i in listing.images) == [DEFAULT_PROJECT, "dev01"]
    image_reads = [p.get("project") for c, p in client.calls if c == "images"]
    assert sorted(image_reads) == [DEFAULT_PROJECT, "dev01"], "qa-cap shares default and is read once through it"


def _route_client(listing_by_project, monkeypatch):
    """The images router with the store read stubbed: what `all_projects` returns and refuses."""
    import contextvars

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.core import org_scope
    from app.core.org_scope import org_scope_dependency
    from app.core.rbac import get_current_user
    from app.routers import images as images_module
    from app.services.lxd_service import get_lxd_service

    class _Service:
        def get_node_names(self):
            return ["nuc2"]

        async def list_images(self, host, project=None):
            return listing_by_project[project]

    monkeypatch.setattr(images_module, "verify_cluster_access", lambda host: None)

    async def _allowed(_svc):
        return None

    monkeypatch.setattr(images_module, "_allowed_image_fingerprints_by_host", _allowed)
    app = FastAPI()
    app.include_router(images_module.router)
    app.dependency_overrides[get_lxd_service] = lambda: _Service()
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    client = TestClient(app, raise_server_exceptions=False)

    def bounded(call, mapping):
        def run():
            org_scope._visible_projects_var.set(mapping)
            org_scope._allowed_clusters_var.set(None)
            return call()

        return contextvars.copy_context().run(run)

    return client, bounded


def test_all_projects_lists_every_store_the_caller_may_see(monkeypatch):
    every = ImageStoreListing(
        images=[_info_from_row(_row(FP_A, project=DEFAULT_PROJECT)), _info_from_row(_row(FP_B, project="dev01"))],
        project=None,
        complete=True,
    )
    client, bounded = _route_client({None: every}, monkeypatch)

    r = client.get("/images/nuc2", params={"all_projects": "true"})
    assert r.status_code == 200, r.text
    assert r.json()["project"] is None and r.json()["complete"] is True
    assert sorted(i["project"] for i in r.json()["images"]) == [DEFAULT_PROJECT, "dev01"]

    r = bounded(lambda: client.get("/images/nuc2", params={"all_projects": "true"}), {"nuc2": {DEFAULT_PROJECT}})
    assert r.status_code == 200, r.text
    assert [i["project"] for i in r.json()["images"]] == [DEFAULT_PROJECT]

    r = client.get("/images/nuc2", params={"all_projects": "true", "project": "dev01"})
    assert r.status_code == 422
