"""A fake daemon and app for the profile router tests, shared by the three test files."""

from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.dependencies import check_read_only_mode
from app.core.org_scope import org_scope_dependency
from app.core.rbac import get_current_user
from app.routers import profiles as profiles_module
from app.services.lxd_service import get_lxd_service


class Response:
    def __init__(self, body: Dict[str, Any], status_code: int = 200,
                 headers: Optional[Dict[str, str]] = None) -> None:
        self._body = body
        self.status_code = status_code
        if headers is not None:
            self.headers = headers

    def json(self) -> Dict[str, Any]:
        return self._body


def sync() -> Response:
    return Response({"type": "sync", "status": "Success", "status_code": 200, "operation": "",
                     "metadata": None}, 200)


def async_op(op: str) -> Response:
    return Response({"type": "async", "status": "Operation created", "status_code": 100,
                     "operation": f"/1.0/operations/{op}", "metadata": {"id": op}}, 202)


class Operations:
    def __init__(self) -> None:
        self.waits: List[Dict[str, Any]] = []
        self.final = "Success"

    def __getitem__(self, op_id: str):
        outer = self

        class _Op:
            @property
            def wait(self):
                class _Wait:
                    def get(self, params=None, timeout=None):
                        outer.waits.append({"op": op_id, "params": params})
                        return Response({"type": "sync", "status": "Success", "status_code": 200,
                                         "metadata": {"id": op_id, "status": outer.final,
                                                      "status_code": 200 if outer.final == "Success" else 400,
                                                      "err": "" if outer.final == "Success" else "the daemon refused it"}})
                return _Wait()
        return _Op()


PROFILE = {"name": "web", "description": "before", "config": {"limits.cpu": "2"},
           "devices": {"eth0": {"type": "nic", "network": "lxdbr0"}}, "used_by": [], "project": "default"}


class Daemon:
    """What the router talks to. Every call is recorded; answers are configurable."""

    def __init__(self, *, etag: Optional[str] = '"abc"', profile: Optional[Dict[str, Any]] = None,
                 put_answer: Optional[Callable[[], Response]] = None,
                 delete_answer: Optional[Callable[[], Response]] = None,
                 post_answer: Optional[Callable[[], Response]] = None) -> None:
        self.etag = etag
        self.profile = dict(profile or PROFILE)
        self.put_answer = put_answer
        self.delete_answer = delete_answer
        self.post_answer = post_answer
        self.posts: List[Dict[str, Any]] = []
        self.puts: List[Dict[str, Any]] = []
        self.deletes: List[Dict[str, Any]] = []
        self.gets: List[str] = []
        self.ops = Operations()

    def get(self, name: str, params=None, timeout=None) -> Response:
        self.gets.append(name)
        headers = {"ETag": self.etag} if self.etag is not None else {}
        return Response({"type": "sync", "metadata": dict(self.profile, name=name)}, 200, headers)

    def put(self, name: str, **kwargs) -> Response:
        self.puts.append({"name": name, **kwargs})
        if self.put_answer is not None:
            return self.put_answer()
        return async_op("op-profile-put")

    def delete(self, name: str, **kwargs) -> Response:
        self.deletes.append({"name": name, **kwargs})
        if self.delete_answer is not None:
            return self.delete_answer()
        return sync()

    def post(self, **kwargs) -> Response:
        self.posts.append(kwargs)
        if self.post_answer is not None:
            return self.post_answer()
        return sync()


def app(monkeypatch, daemon: Daemon):
    """A TestClient over the profile router bound to `daemon`; returns (client, calls)."""
    calls = {"activity": [], "invalidated": []}

    class _Profile:
        def __init__(self, name: str) -> None:
            self.name = name

        def get(self, params=None, timeout=None):
            return daemon.get(self.name, params=params, timeout=timeout)

        def put(self, **kwargs):
            return daemon.put(self.name, **kwargs)

        def delete(self, **kwargs):
            return daemon.delete(self.name, **kwargs)

    class _Profiles:
        def post(self, **kwargs):
            return daemon.post(**kwargs)

        def __getitem__(self, name):
            return _Profile(name)

    class _Api:
        operations = daemon.ops

        def __getitem__(self, key):
            return {"profiles": _Profiles()}[key]

    client = SimpleNamespace(api=_Api())
    svc = SimpleNamespace(get_client_with_reconnect=lambda host: client)
    monkeypatch.setattr(profiles_module, "resolve_owner", lambda c, kind, project: "default")
    monkeypatch.setattr(profiles_module, "project_is_visible", lambda host, project: True)
    monkeypatch.setattr(profiles_module, "verify_cluster_access", lambda host: None, raising=False)
    monkeypatch.setattr(profiles_module, "filter_visible_lxd_references", lambda host, refs: refs)
    monkeypatch.setattr(profiles_module, "tenant_acl_active", lambda: False)
    monkeypatch.setattr(profiles_module.activity_logger, "log_activity",
                        lambda **kw: calls["activity"].append(kw))
    monkeypatch.setattr(
        profiles_module, "get_cache_manager",
        lambda: SimpleNamespace(invalidate_resource_cache=lambda *k: calls["invalidated"].append(k),
                                delete_pattern=lambda *a, **k: None),
    )
    fastapi_app = FastAPI()
    fastapi_app.include_router(profiles_module.router)
    fastapi_app.dependency_overrides[get_lxd_service] = lambda: svc
    fastapi_app.dependency_overrides[get_current_user] = lambda: None
    fastapi_app.dependency_overrides[org_scope_dependency] = lambda: None
    fastapi_app.dependency_overrides[check_read_only_mode] = lambda: None
    return TestClient(fastapi_app, raise_server_exceptions=False), calls
