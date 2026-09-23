"""
An ACL rule's ports are checked before the daemon sees them.

The dialog passed "Destination port" through untouched and had no source-port field at all,
so LXD's refusal of a bad port arrived as its raw text through a 500, and a port on an ICMP
rule (which LXD refuses outright) was only discovered after a round trip. The rule LXD applies
(a port, a comma list, or a low-high range, 1..65535, only with tcp or udp) is applied here on
create and on update, with the direction, rule number and field named.
"""

from types import SimpleNamespace
from typing import Any, Dict, List
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.dependencies import check_read_only_mode
from app.core.org_scope import org_scope_dependency
from app.core.rbac import get_current_user
from app.routers import network_acls as acls_module
from app.services.lxd.acl_rules import port_list_error, rule_error
from app.services.lxd_service import get_lxd_service


@asynccontextmanager
async def _no_configuration_lock(_db, _host):
    yield


class _Response:
    def __init__(self, body: Dict[str, Any], status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code
        self.headers: Dict[str, str] = {}

    def json(self) -> Dict[str, Any]:
        return self._body


def _sync() -> _Response:
    return _Response({"type": "sync", "status": "Success", "status_code": 200, "operation": "",
                      "metadata": None}, 200)


def _app(monkeypatch):
    calls: List[Dict[str, Any]] = []

    class _Acl:
        def get(self, params=None, timeout=None):
            return _Response({"type": "sync", "metadata": {
                "name": "qaacl", "description": "", "ingress": [], "egress": [], "config": {},
                "used_by": [], "project": "default"}})

        def put(self, **kwargs):
            calls.append({"put": kwargs["json"]})
            return _sync()

    class _Acls:
        def post(self, json, params, timeout=None):
            calls.append({"post": json})
            return _sync()

        def __getitem__(self, name):
            return _Acl()

    class _Api:
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
    return TestClient(app, raise_server_exceptions=False), calls


def _rule(**kw) -> Dict[str, Any]:
    return {"action": "allow", "protocol": "tcp", "state": "enabled", **kw}


def test_the_port_syntax_lxd_accepts_is_accepted() -> None:
    for good in ("80", "80,443", "8000-8100", "1024-65535", "22, 80", "1", "65535"):
        assert port_list_error(good) is None, good


def test_the_port_syntax_lxd_refuses_is_refused_with_a_reason() -> None:
    for bad in ("0", "70000", "443-80", "80,", ",80", "http", "80-", "-80", "80-80", "1-2-3", " "):
        assert port_list_error(bad), bad


def test_a_port_needs_tcp_or_udp() -> None:
    assert rule_error(_rule(destination_port="80")) is None
    assert rule_error(_rule(protocol="udp", source_port="53")) is None
    assert "protocol" in (rule_error(_rule(protocol="icmp4", destination_port="80")) or "")
    assert "protocol" in (rule_error(_rule(protocol="", source_port="80")) or "")
    assert "protocol" in (rule_error({"action": "allow", "destination_port": "80"}) or "")
    assert rule_error(_rule(protocol="icmp4")) is None


def test_a_create_with_a_bad_port_never_reaches_the_daemon(monkeypatch) -> None:
    tc, calls = _app(monkeypatch)
    for bad in ("0", "70000", "443-80", "80,", "http"):
        r = tc.post("/network-acls?host=nuc1", json={
            "name": "qaacl", "ingress": [_rule(destination_port=bad)], "egress": []})
        assert r.status_code == 422, (bad, r.text)
        assert "ingress rule 1" in r.text and "destination_port" in r.text, r.text
    r = tc.post("/network-acls?host=nuc1", json={
        "name": "qaacl", "ingress": [], "egress": [_rule(protocol="icmp4", source_port="80")]})
    assert r.status_code == 422, r.text
    assert "egress rule 1" in r.text and "source_port" in r.text, r.text
    assert calls == [], "nothing was sent to the daemon"


def test_a_good_port_list_is_forwarded_untouched(monkeypatch) -> None:
    tc, calls = _app(monkeypatch)
    rule = _rule(source_port="1024-65535", destination_port="80,443,8000-8100")
    r = tc.post("/network-acls?host=nuc1", json={"name": "qaacl", "ingress": [rule], "egress": []})
    assert r.status_code == 200, r.text
    assert calls[0]["post"]["ingress"][0]["source_port"] == "1024-65535"
    assert calls[0]["post"]["ingress"][0]["destination_port"] == "80,443,8000-8100"


def test_an_update_with_a_bad_port_never_reaches_the_daemon(monkeypatch) -> None:
    tc, calls = _app(monkeypatch)
    r = tc.put("/network-acls/qaacl?host=nuc1", json={"egress": [_rule(destination_port="70000")]})
    assert r.status_code == 422, r.text
    assert "egress rule 1" in r.text
    assert calls == []
    r = tc.put("/network-acls/qaacl?host=nuc1", json={"egress": [_rule(destination_port="443")]})
    assert r.status_code == 200, r.text
    assert calls[0]["put"]["egress"][0]["destination_port"] == "443"
