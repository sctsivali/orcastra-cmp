"""
An ACL edit carries the ETag it was read with, and a stale one is refused with a 412.

Without a precondition, two operators editing one ACL end with the later save winning whole.
The single read answers the daemon's ETag, the PUT forwards If-Match, and a mismatch (the
daemon's or one the fresh read already shows) is a 412 with a sentence an operator can act on.
"""

from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pylxd import exceptions as pylxd_exceptions

from app.core.dependencies import check_read_only_mode
from app.core.org_scope import org_scope_dependency
from app.core.rbac import get_current_user
from app.routers import network_acls as acls_module
from app.services.lxd import utils as lxd_utils
from app.services.lxd_service import get_lxd_service


@asynccontextmanager
async def _no_configuration_lock(_db, _host):
    yield


class _Response:
    def __init__(self, body: Dict[str, Any], status_code: int = 200,
                 headers: Optional[Dict[str, str]] = None) -> None:
        self._body = body
        self.status_code = status_code
        if headers is not None:
            self.headers = headers

    def json(self) -> Dict[str, Any]:
        return self._body


ACL = {"name": "qaacl", "description": "before", "ingress": [{"action": "allow"}], "egress": [],
       "config": {}, "used_by": [], "project": "default"}


def _async(op: str) -> _Response:
    return _Response({"type": "async", "status": "Operation created", "status_code": 100,
                      "operation": f"/1.0/operations/{op}", "metadata": {"id": op}}, 202)


class _Operations:
    def __init__(self) -> None:
        self.waits: List[Dict[str, Any]] = []

    def __getitem__(self, op_id: str):
        outer = self

        class _Op:
            @property
            def wait(self):
                class _Wait:
                    def get(self, params=None, timeout=None):
                        outer.waits.append({"op": op_id, "params": params})
                        return _Response({"type": "sync", "status": "Success", "status_code": 200,
                                          "metadata": {"id": op_id, "status": "Success",
                                                       "status_code": 200, "err": ""}})
                return _Wait()
        return _Op()


def _app(monkeypatch, *, etag: Optional[str] = '"abc"', put_answer=None):
    ops = _Operations()
    puts: List[Dict[str, Any]] = []

    class _Acl:
        def get(self, params=None, timeout=None):
            headers = {"ETag": etag} if etag is not None else {}
            return _Response({"type": "sync", "metadata": dict(ACL)}, 200, headers)

        def put(self, **kwargs):
            puts.append(kwargs)
            if put_answer is not None:
                return put_answer()
            return _async("op-acl-put")

    class _Acls:
        def __getitem__(self, name):
            return _Acl()

    class _Api:
        operations = ops

        def __getitem__(self, key):
            return {"network-acls": _Acls()}[key]

    client = SimpleNamespace(api=_Api())
    svc = SimpleNamespace(connection_manager=SimpleNamespace(get_client=lambda host: client))
    monkeypatch.setattr(acls_module, "require_client", lambda c, host: c)
    monkeypatch.setattr(acls_module, "resolve_owner", lambda c, kind, project: "default")
    monkeypatch.setattr(acls_module, "project_is_visible", lambda host, project: True)
    monkeypatch.setattr(acls_module, "verify_cluster_access", lambda host: None, raising=False)
    monkeypatch.setattr(
        acls_module,
        "serialize_cluster_configuration",
        _no_configuration_lock,
    )
    monkeypatch.setattr(acls_module, "filter_visible_lxd_references", lambda host, refs: refs)
    monkeypatch.setattr(acls_module, "tenant_acl_active", lambda: False)
    monkeypatch.setattr(acls_module.activity_logger, "log_activity", lambda **kw: None)
    monkeypatch.setattr(
        acls_module, "get_cache_manager",
        lambda: SimpleNamespace(invalidate_resource_cache=lambda *a, **k: None,
                                delete_pattern=lambda *a, **k: None),
        raising=False,
    )
    app = FastAPI()
    app.include_router(acls_module.router)
    app.dependency_overrides[get_lxd_service] = lambda: svc
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    app.dependency_overrides[check_read_only_mode] = lambda: None
    return TestClient(app, raise_server_exceptions=False), puts, ops


def test_the_single_read_carries_the_daemons_etag(monkeypatch) -> None:
    tc, _, _ = _app(monkeypatch)
    r = tc.get("/network-acls/qaacl?host=nuc1")
    assert r.status_code == 200, r.text
    assert r.json()["etag"] == '"abc"'
    assert r.headers.get("etag") == '"abc"'


def test_a_daemon_without_an_etag_answers_null_not_a_500(monkeypatch) -> None:
    tc, _, _ = _app(monkeypatch, etag=None)
    r = tc.get("/network-acls/qaacl?host=nuc1")
    assert r.status_code == 200, r.text
    assert r.json()["etag"] is None


def test_an_update_forwards_if_match_and_still_waits_for_the_operation(monkeypatch) -> None:
    tc, puts, ops = _app(monkeypatch)
    r = tc.put("/network-acls/qaacl?host=nuc1", json={"description": "after"},
               headers={"If-Match": '"abc"'})
    assert r.status_code == 200, r.text
    assert len(puts) == 1
    assert puts[0]["headers"] == {"If-Match": '"abc"'}
    assert puts[0]["timeout"] == lxd_utils.write_timeout(acls_module.ACL_WRITE_WAIT_SECONDS)
    assert puts[0]["json"]["description"] == "after"
    assert [w["op"] for w in ops.waits] == ["op-acl-put"]


def test_a_stale_etag_the_daemon_refuses_is_a_412_not_a_500(monkeypatch) -> None:
    def refused():
        raise pylxd_exceptions.LXDAPIException(
            _Response({"type": "error", "error": "ETag doesn't match: a vs b", "error_code": 412}, 412))
    # The local comparison passes (same ETag); the daemon is the one that refuses.
    tc, puts, ops = _app(monkeypatch, put_answer=refused)
    r = tc.put("/network-acls/qaacl?host=nuc1", json={"description": "after"},
               headers={"If-Match": '"abc"'})
    assert r.status_code == 412, r.text
    assert "changed since" in r.json()["detail"]
    assert len(puts) == 1
    assert ops.waits == []


def test_a_stale_etag_the_fresh_read_already_contradicts_never_reaches_the_daemon(monkeypatch) -> None:
    tc, puts, ops = _app(monkeypatch, etag='"new"')
    r = tc.put("/network-acls/qaacl?host=nuc1", json={"description": "after"},
               headers={"If-Match": '"old"'})
    assert r.status_code == 412, r.text
    assert "changed since" in r.json()["detail"]
    assert puts == []
    assert ops.waits == []


def test_an_update_without_the_header_is_an_unconditional_write(monkeypatch) -> None:
    tc, puts, _ = _app(monkeypatch)
    r = tc.put("/network-acls/qaacl?host=nuc1", json={"description": "after"})
    assert r.status_code == 200, r.text
    assert "headers" not in puts[0]


def test_quotes_around_the_etag_do_not_matter(monkeypatch) -> None:
    """LXD compares after trimming the quotes; a client that strips them must not be refused."""
    tc, puts, _ = _app(monkeypatch, etag='"abc"')
    r = tc.put("/network-acls/qaacl?host=nuc1", json={"description": "after"},
               headers={"If-Match": "abc"})
    assert r.status_code == 200, r.text
    assert len(puts) == 1
