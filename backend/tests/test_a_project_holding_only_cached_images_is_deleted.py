"""A project whose only leftovers are cached images is deleted, on Incus as on LXD.

Found on the Incus lab: a project emptied of its instances still could not be deleted, "Only
empty projects can be removed". The image the daemon cached for the instance's create stayed in
the project's store. LXD 5.21 skips cached image URLs when it judges a project empty and removes
them with it (lxd/api_project.go, projectIsEmpty's skipURLs); Incus 7.6 counts them
(cmd/incusd/api_project.go projectIsEmpty ignores only the default profile).

So after the daemon refuses, the project is read: when everything left is the default profile
and images the daemon itself cached, those images go and the delete is sent once more. An image
someone uploaded or named is never removed, and the refusal stands.
"""

from __future__ import annotations

import pytest
from pylxd import exceptions as lxd_exceptions

from app.services.lxd import project_delete
from app.services.lxd.exceptions import LXDOperationError

CACHED = "c" * 64
UPLOADED = "u" * 64


class _Response:
    def __init__(self, status: int, body):
        self.status_code = status
        self._body = body
        self.content = b"{}"
        self.text = "{}"
        self.headers = {}

    def json(self):
        return self._body


class _Daemon:
    def __init__(self, images: dict):
        self.images = dict(images)  # fingerprint -> cached
        self.deleted_images: list[str] = []
        self.project_deletes = 0
        self.project_gone = False

    def used_by(self):
        return ["/1.0/profiles/default?project=web"] + [f"/1.0/images/{fp}?project=web" for fp in self.images]


class _Client:
    def __init__(self, daemon: _Daemon):
        d = daemon

        class _Project:
            def get(self, timeout=None):
                return _Response(200, {"type": "sync", "metadata": {"name": "web", "used_by": d.used_by(), "config": {"features.images": "true"}}})

            def delete(self, timeout=None):
                d.project_deletes += 1
                if d.images:
                    raise lxd_exceptions.LXDAPIException(_Response(400, {"error": "Only empty projects can be removed"}))
                d.project_gone = True
                return _Response(200, {"type": "sync", "metadata": {}})

        class _Projects:
            def __getitem__(self, name):
                return _Project()

        class _Image:
            def __init__(self, fp):
                self.fp = fp

            def delete(self, params=None, timeout=None):
                d.deleted_images.append(self.fp)
                d.images.pop(self.fp, None)
                return _Response(200, {"type": "sync", "metadata": {}})

        class _Images:
            def get(self, params=None, timeout=None):
                return _Response(200, {"type": "sync", "metadata": [{"fingerprint": fp, "cached": c} for fp, c in d.images.items()]})

            def __getitem__(self, fp):
                return _Image(fp)

        self.api = type("api", (), {"projects": _Projects(), "images": _Images()})()


def test_a_project_left_with_a_cached_image_is_deleted():
    daemon = _Daemon({CACHED: True})
    project_delete.delete_project_sync(lambda host: _Client(daemon), "incus-lab", "web")
    assert daemon.deleted_images == [CACHED] and daemon.project_gone and daemon.project_deletes == 2


def test_an_uploaded_image_is_never_removed_to_make_room():
    daemon = _Daemon({CACHED: True, UPLOADED: False})
    with pytest.raises(LXDOperationError, match="Only empty projects"):
        project_delete.delete_project_sync(lambda host: _Client(daemon), "incus-lab", "web")
    assert daemon.deleted_images == [] and not daemon.project_gone


def test_an_empty_project_is_deleted_in_one_call():
    daemon = _Daemon({})
    project_delete.delete_project_sync(lambda host: _Client(daemon), "incus-lab", "web")
    assert daemon.project_deletes == 1 and daemon.deleted_images == []
