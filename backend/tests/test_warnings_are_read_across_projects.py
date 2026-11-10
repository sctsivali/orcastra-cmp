"""Warnings are read from every project; a daemon that refuses `all-projects` is asked without it.

`/1.0/warnings` without `all-projects` answers about the default project only, so a warning in
any other project would never reach the page. The row keeps the project LXD stamps on it, which
the tenant filter keys on.
"""

from typing import Any

from pylxd.exceptions import LXDAPIException

from app.routers.lxd_warnings import _fetch_warnings_for_node

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

    def get(self, params=None, timeout=None):
        return self._handler(self._path, params or {})


class _Client:
    project = None

    def __init__(self, handler):
        self.api = _APINode("/1.0", handler)
        self.host_info = {"api_extensions": ["warnings"]}


class _Service:
    def __init__(self, client):
        self._client = client
        self.connection_manager = self

    def get_client(self, host):
        return self._client


ROW = {"uuid": "u1", "type": "Instance type not operational", "status": "new", "severity": "moderate",
       "location": "member-b", "project": "uat-hermes", "count": 3,
       "first_seen_at": "2026-09-01T00:00:00Z", "last_seen_at": "2026-09-11T00:00:00Z", "last_message": "m"}


def test_every_project_is_asked_for_and_the_row_keeps_its_project():
    calls = []

    def handler(path, params):
        calls.append(dict(params))
        assert path == "/1.0/warnings"
        return _Resp(200, {"metadata": [ROW]})

    read = _fetch_warnings_for_node(_Service(_Client(handler)), HOST)
    assert calls == [{"recursion": "1", "all-projects": "true"}]
    assert [(w.uuid, w.project, w.location, w.source_node, w.count) for w in read.rows] == [
        ("u1", "uat-hermes", "member-b", HOST, 3)
    ]
    assert read.complete


def test_a_daemon_that_refuses_the_parameter_is_asked_the_old_way():
    calls = []

    def handler(path, params):
        calls.append(dict(params))
        if "all-projects" in params:
            raise LXDAPIException(_Resp(400, {"error": "Invalid query parameter"}))
        return _Resp(200, {"metadata": [dict(ROW, project="", location="none")]})

    read = _fetch_warnings_for_node(_Service(_Client(handler)), HOST)
    assert calls == [{"recursion": "1", "all-projects": "true"}, {"recursion": "1"}]
    assert [(w.project, w.location) for w in read.rows] == [("default", HOST)]
