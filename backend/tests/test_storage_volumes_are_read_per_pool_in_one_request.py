"""Volumes are read one request per pool, across every project, with no per-volume sync.

pylxd's collection manager listed a pool's volumes at recursion 0 in the default project and
then synced every volume with its own GET. On a clustered host a per-member pool lists its
volumes as `/1.0/storage-pools/local/volumes/custom/backups?target=microcloud20`, which the
per-volume sync could not read, and the first refusal dropped the rest of that pool. On a
production MicroCloud the page showed zero volumes and zero ISOs for a cluster holding 140.

Now one `?recursion=1&all-projects=true` per pool where LXD supports it, one read per owning
project otherwise, every row keeping its project and member.
"""

from typing import Any, Dict, List

import pytest
from pylxd.exceptions import LXDAPIException

from app.routers.storage_volumes import _fetch_volumes_for_node
from app.services.lxd.fanout import NodeRead
from app.services.lxd.project_scope import list_pool_volumes

HOST = "C2"


class _Resp:
    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self._body = body
        self.content = b""

    def json(self):
        return self._body


class _APINode:
    def __init__(self, path, handler):
        self._path, self._handler = path, handler

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return _APINode(f"{self._path}/{name.replace('_', '-')}", self._handler)

    def __getitem__(self, item):
        return _APINode(f"{self._path}/{item}", self._handler)

    def get(self, params=None, timeout=None):
        return self._handler(self._path, params or {})


class _Client:
    """No `storage_pools` manager on purpose: any lazy pylxd path fails loudly."""

    project = None

    def __init__(self, handler, extensions):
        self.api = _APINode("/1.0", handler)
        self.host_info = {"api_extensions": list(extensions)}


class _Service:
    """The two attributes the helper touches: connection_manager.get_client."""

    def __init__(self, client):
        self._client = client
        self.connection_manager = self

    def get_client(self, host):
        return self._client


LOCAL_ROWS = [
    {"name": "backups", "type": "custom", "content_type": "filesystem", "project": "default",
     "location": "microcloud20", "config": {}, "used_by": []},
    {"name": "backups", "type": "custom", "content_type": "filesystem", "project": "default",
     "location": "microcloud30", "config": {}, "used_by": []},
    {"name": "web01", "type": "container", "content_type": "filesystem", "project": "atlas-desktop-lab",
     "location": "microcloud40", "config": {}, "used_by": ["/1.0/instances/web01?project=atlas-desktop-lab"]},
]
REMOTE_ROWS = [
    {"name": "FreeBSD-14.2.iso", "type": "custom", "content_type": "iso", "project": "default",
     "location": "none", "config": {}, "used_by": []},
]


def _cluster(calls: List[Dict[str, Any]]):
    def handler(path, params):
        calls.append({"path": path, "params": dict(params)})
        if path == "/1.0/storage-pools":
            return _Resp(200, {"metadata": ["/1.0/storage-pools/local", "/1.0/storage-pools/remote"]})
        if path == "/1.0/storage-pools/local/volumes":
            assert params.get("all-projects") == "true" and params.get("recursion") == 1
            return _Resp(200, {"metadata": LOCAL_ROWS})
        if path == "/1.0/storage-pools/remote/volumes":
            return _Resp(200, {"metadata": REMOTE_ROWS})
        raise AssertionError(f"unexpected read {path} {params}")
    return _Client(handler, ["storage_volumes_all_projects"])


def test_one_request_per_pool_with_project_and_member_on_every_row():
    calls: List[Dict[str, Any]] = []
    read = _fetch_volumes_for_node(_Service(_cluster(calls)), HOST, None)
    assert isinstance(read, NodeRead) and read.complete
    listed = [c["path"] for c in calls if c["path"].endswith("/volumes")]
    assert listed == ["/1.0/storage-pools/local/volumes", "/1.0/storage-pools/remote/volumes"]
    rows = {(r.pool, r.type, r.name, r.location, r.project) for r in read.rows}
    assert ("local", "custom", "backups", "microcloud20", "default") in rows
    assert ("local", "custom", "backups", "microcloud30", "default") in rows
    assert ("local", "container", "web01", "microcloud40", "atlas-desktop-lab") in rows
    assert ("remote", "custom", "FreeBSD-14.2.iso", "", "default") in rows, "shared pool: location is empty, not 'none'"
    iso = [r for r in read.rows if r.content_type == "iso"]
    assert [r.name for r in iso] == ["FreeBSD-14.2.iso"]
    assert len(calls) == 3, "pool list plus one listing per pool, nothing synced"


def test_a_pool_filter_reads_only_that_pool():
    calls: List[Dict[str, Any]] = []
    read = _fetch_volumes_for_node(_Service(_cluster(calls)), HOST, "remote")
    assert [c["path"] for c in calls] == ["/1.0/storage-pools/remote/volumes"]
    assert [r.name for r in read.rows] == ["FreeBSD-14.2.iso"]


def test_a_pool_that_refuses_is_named_and_the_others_still_list():
    calls: List[Dict[str, Any]] = []

    def handler(path, params):
        calls.append(path)
        if path == "/1.0/storage-pools":
            return _Resp(200, {"metadata": ["/1.0/storage-pools/local", "/1.0/storage-pools/remote"]})
        if path == "/1.0/storage-pools/local/volumes":
            raise LXDAPIException(_Resp(400, {"error": "More than one cluster member has a volume named backups"}))
        if path == "/1.0/storage-pools/remote/volumes":
            return _Resp(200, {"metadata": REMOTE_ROWS})
        raise AssertionError(path)

    read = _fetch_volumes_for_node(_Service(_Client(handler, ["storage_volumes_all_projects"])), HOST, None)
    assert [r.name for r in read.rows] == ["FreeBSD-14.2.iso"]
    assert not read.complete
    assert read.problems == ["pool local: LXD answered 400: More than one cluster member has a volume named backups"]


def test_without_the_extension_each_owning_project_is_read_once():
    """features.storage.volumes false means the project sees default's volumes: read default once."""
    calls: List[Dict[str, Any]] = []

    def handler(path, params):
        calls.append({"path": path, "params": dict(params)})
        if path == "/1.0/projects" and params.get("recursion") == 0:
            return _Resp(200, {"metadata": ["/1.0/projects/default", "/1.0/projects/shares-default", "/1.0/projects/own"]})
        if path == "/1.0/projects" and params.get("recursion") == 1:
            return _Resp(200, {"metadata": [
                {"name": "default", "config": {}},
                {"name": "shares-default", "config": {"features.storage.volumes": "false"}},
                {"name": "own", "config": {"features.storage.volumes": "true"}},
            ]})
        if path == "/1.0/storage-pools/local/volumes":
            assert "all-projects" not in params
            project = params["project"]
            return _Resp(200, {"metadata": [{"name": f"vol-{project}", "type": "custom", "content_type": "filesystem",
                                             "location": "none", "config": {}, "used_by": []}]})
        raise AssertionError(f"{path} {params}")

    rows, complete = list_pool_volumes(_Client(handler, []), "local")
    assert complete
    projects_read = [c["params"]["project"] for c in calls if c["path"].endswith("/volumes")]
    assert sorted(projects_read) == ["default", "own"], projects_read
    assert sorted((r["name"], r["project"]) for r in rows) == [("vol-default", "default"), ("vol-own", "own")]
