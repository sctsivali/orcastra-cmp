"""Storage pools are read with one recursion=1 request, never a lazy sync per pool.

pylxd's manager lists pools at recursion 0 and then syncs each pool on first attribute access,
one request per pool, each another chance for a transport error to read as "no pools". A pool
is a cluster-wide object: one listing carries name, driver, status and config for all of them.
"""

from typing import Any

from app.routers.storage_pools import _fetch_pools_for_node
from app.services.lxd.fanout import NodeRead

HOST = "C7"


class _Resp:
    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self._body = body

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
    """Has no `storage_pools` manager, so a lazy pylxd path raises."""

    project = None

    def __init__(self, handler):
        self.api = _APINode("/1.0", handler)
        self.host_info = {"api_extensions": []}


class _Service:
    def __init__(self, client):
        self._client = client

    def get_client_with_reconnect(self, host):
        return self._client


POOLS = [
    {"name": "local", "driver": "zfs", "status": "Created", "description": "", "config": {"size": "1.7TiB"},
     "used_by": ["/1.0/instances/a"], "locations": ["member-a", "member-b", "member-c"]},
    {"name": "remote", "driver": "ceph", "status": "Created", "description": "shared", "config": {},
     "used_by": [], "locations": ["member-a", "member-b", "member-c"]},
    {"name": "sata-ssd", "driver": "ceph", "status": "Created", "description": "", "config": {},
     "used_by": [], "locations": ["member-a", "member-b", "member-c"]},
]


def test_three_pools_from_one_request():
    calls = []

    def handler(path, params):
        calls.append((path, dict(params)))
        assert path == "/1.0/storage-pools", path
        return _Resp(200, {"metadata": POOLS})

    read = _fetch_pools_for_node(_Service(_Client(handler)), HOST)
    assert isinstance(read, NodeRead) and read.complete
    assert calls == [("/1.0/storage-pools", {"recursion": 1})]
    assert [(p.name, p.driver, p.status, p.node) for p in read.rows] == [
        ("local", "zfs", "Created", HOST),
        ("remote", "ceph", "Created", HOST),
        ("sata-ssd", "ceph", "Created", HOST),
    ]
    assert read.rows[0].config == {"size": "1.7TiB"} and read.rows[0].used_by == ["/1.0/instances/a"]
    assert read.rows[1].description == "shared"


def test_a_row_lxd_did_not_fill_in_still_lists():
    def handler(path, params):
        return _Resp(200, {"metadata": [{"name": "dir1", "driver": "dir", "config": None, "used_by": None}]})

    read = _fetch_pools_for_node(_Service(_Client(handler)), HOST)
    assert [(p.name, p.status, p.config, p.used_by) for p in read.rows] == [("dir1", "", {}, [])]
