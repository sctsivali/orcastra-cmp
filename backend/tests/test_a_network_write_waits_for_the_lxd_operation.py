"""A network, ACL or pool write on LXD 6.9 is an operation, and the console waits for it.

The storage_and_network_operations API extension (present on LXD 6.9) turns create, update
and delete on networks, network ACLs and storage pools into background operations: the daemon
answers 202 with an operation and finishes afterwards. The console read a network back
straight after its POST and got "Network not found" for a network the daemon was still
creating, so the operator saw an error while the network appeared on the host a moment later,
and a delete answered before the row was gone. Every write now waits on the operation it got,
in slices shorter than the pooled client's read timeout; an older daemon answers
synchronously and the wait is a no-op.
"""

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pylxd import exceptions as pylxd_exceptions

from app.core.dependencies import check_read_only_mode
from app.core.org_scope import org_scope_dependency
from app.core.rbac import get_current_user
from app.routers import network_acls as acls_module
from app.routers import networks as networks_module
from app.services.lxd import utils as lxd_utils
from app.services.lxd_service import get_lxd_service


class _Response:
    def __init__(self, body: Dict[str, Any], status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code

    def json(self) -> Dict[str, Any]:
        return self._body


def _async(op: str) -> _Response:
    return _Response({"type": "async", "status": "Operation created", "status_code": 100,
                      "operation": f"/1.0/operations/{op}", "metadata": {"id": op}}, 202)


def _sync() -> _Response:
    return _Response({"type": "sync", "status": "Success", "status_code": 200, "operation": "",
                      "metadata": None}, 200)


def _deadline_error() -> pylxd_exceptions.LXDAPIException:
    """What LXD 5.21 and 6.x answer when /operations/{id}/wait?timeout=N elapses: HTTP 200
    with an error body, which pylxd raises the same way it raises a failed request."""
    return pylxd_exceptions.LXDAPIException(_Response({
        "type": "error", "status": "", "status_code": 0, "operation": "",
        "error_code": 500, "error": "context deadline exceeded", "metadata": None,
    }))


class _Operations:
    """client.api.operations[op]: `.wait.get` answers Running until `settle_after` waits, then
    Success. With `deadline_errors`, a wait whose slice elapses raises the way LXD really
    answers, and `.get()` on the operation reports its current status; `vanished` makes that
    read a 404, the way a finished operation that LXD already dropped looks."""

    def __init__(self, settle_after: int = 1, final: str = "Success",
                 deadline_errors: bool = False, vanished: bool = False) -> None:
        self.waits: List[Dict[str, Any]] = []
        self.reads: List[str] = []
        self.settle_after = settle_after
        self.final = final
        self.deadline_errors = deadline_errors
        self.vanished = vanished

    def _status(self) -> str:
        return self.final if len(self.waits) >= self.settle_after else "Running"

    def __getitem__(self, op: str):
        def wait_get(params: Optional[Dict[str, Any]] = None) -> _Response:
            self.waits.append({"op": op, "params": dict(params or {})})
            status = self._status()
            if status == "Running" and self.deadline_errors:
                raise _deadline_error()
            err = "boom" if status == "Failure" else ""
            return _Response({"type": "sync", "metadata": {"id": op, "status": status, "err": err}})

        def get() -> _Response:
            self.reads.append(op)
            if self.vanished:
                raise pylxd_exceptions.NotFound(_Response({"type": "error", "error": "not found"}, 404))
            return _Response({"type": "sync", "metadata": {"id": op, "status": self._status(), "err": ""}})

        return SimpleNamespace(wait=SimpleNamespace(get=wait_get), get=get)


class _Networks:
    """client.api.networks: the read-back 404s until the operation has been waited on."""

    def __init__(self, operations: _Operations, async_writes: bool) -> None:
        self.ops = operations
        self.async_writes = async_writes
        self.posts: List[Dict[str, Any]] = []
        self.deletes: List[str] = []
        self.delete_timeouts: List[Any] = []
        self.gets_before_settled = 0

    def post(self, json: Dict[str, Any], params: Dict[str, Any], timeout=None) -> _Response:
        self.posts.append({"json": json, "params": dict(params), "timeout": timeout})
        return _async("op-create") if self.async_writes else _sync()

    def __getitem__(self, name: str):
        def get(params=None):
            settled = (not self.async_writes) or any(w["op"] == "op-create" for w in self.ops.waits)
            if not settled:
                self.gets_before_settled += 1
                raise pylxd_exceptions.LXDAPIException(_Response({"error": "Network not found"}, 404))
            return _Response({"metadata": {"name": name, "type": "bridge", "managed": True, "config": {}}})

        def delete(params=None, timeout=None):
            self.deletes.append(name)
            self.delete_timeouts.append(timeout)
            return _async("op-delete") if self.async_writes else _sync()

        return SimpleNamespace(get=get, delete=delete)


def _networks_app(monkeypatch, async_writes: bool):
    ops = _Operations()
    nets = _Networks(ops, async_writes)
    client = SimpleNamespace(api=SimpleNamespace(networks=nets, operations=ops))
    svc = SimpleNamespace(get_client_with_reconnect=lambda host: client)
    monkeypatch.setattr(networks_module, "_is_clustered", lambda c: False)
    monkeypatch.setattr(networks_module, "list_project_configs", lambda c: {})
    monkeypatch.setattr(networks_module, "resolve_owner", lambda c, kind, project: project or "default")
    monkeypatch.setattr(networks_module, "project_is_visible", lambda host, project: True)
    monkeypatch.setattr(networks_module, "_uplink_candidates", lambda c, host: {})
    monkeypatch.setattr(networks_module, "verify_cluster_access", lambda host: None)
    monkeypatch.setattr(networks_module.activity_logger, "log_activity", lambda **kw: None)
    monkeypatch.setattr(
        networks_module, "get_cache_manager",
        lambda: SimpleNamespace(invalidate_resource_cache=lambda *a, **k: None,
                                delete_pattern=lambda *a, **k: None),
        raising=False,
    )
    app = FastAPI()
    app.include_router(networks_module.router)
    app.dependency_overrides[get_lxd_service] = lambda: svc
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    app.dependency_overrides[check_read_only_mode] = lambda: None
    return TestClient(app, raise_server_exceptions=False), nets, ops


BRIDGE = {"name": "qanet1", "type": "bridge", "config": {"ipv4.address": "auto", "ipv6.address": "none"}}


def test_a_create_that_got_an_operation_waits_before_reading_the_network_back(monkeypatch) -> None:
    client, nets, ops = _networks_app(monkeypatch, async_writes=True)
    r = client.post("/networks?host=nuc1", json=BRIDGE)
    assert r.status_code == 200, r.text
    assert r.json()["network"]["name"] == "qanet1"
    assert [w["op"] for w in ops.waits] == ["op-create"]
    assert nets.gets_before_settled == 0


def test_a_synchronous_daemon_is_not_waited_on(monkeypatch) -> None:
    client, nets, ops = _networks_app(monkeypatch, async_writes=False)
    r = client.post("/networks?host=nuc1", json=BRIDGE)
    assert r.status_code == 200, r.text
    assert ops.waits == []


def test_a_delete_that_got_an_operation_waits_for_it(monkeypatch) -> None:
    client, nets, ops = _networks_app(monkeypatch, async_writes=True)
    r = client.delete("/networks/qanet1?host=nuc1")
    assert r.status_code == 200, r.text
    assert nets.deletes == ["qanet1"]
    assert [w["op"] for w in ops.waits] == ["op-delete"]


def test_a_failed_operation_is_an_lxd_error_not_a_success(monkeypatch) -> None:
    client, nets, ops = _networks_app(monkeypatch, async_writes=True)
    ops.final = "Failure"
    r = client.post("/networks?host=nuc1", json=BRIDGE)
    assert r.status_code != 200
    assert nets.gets_before_settled == 0


def test_the_wait_is_taken_in_slices_shorter_than_the_pooled_read_timeout() -> None:
    ops = _Operations(settle_after=3)
    client = SimpleNamespace(api=SimpleNamespace(operations=ops))
    lxd_utils.wait_for_lxd_operation(client, _async("op-x"), timeout=12)
    assert [w["params"]["timeout"] for w in ops.waits] == [5, 5, 2]
    for w in ops.waits:
        assert w["params"]["timeout"] < lxd_utils.OPERATION_WAIT_SLICE_SECONDS + 1


def test_the_wait_stops_at_the_first_terminal_status() -> None:
    ops = _Operations(settle_after=1)
    client = SimpleNamespace(api=SimpleNamespace(operations=ops))
    lxd_utils.wait_for_lxd_operation(client, _async("op-y"), timeout=60)
    assert len(ops.waits) == 1


def _acl_app(monkeypatch, post_answer=None):
    """A TestClient over the ACL router with a fake daemon; `post_answer` overrides the create."""
    ops = _Operations()
    calls: List[str] = []

    class _Acls:
        def post(self, json, params, timeout=None):
            calls.append("post")
            calls.append(f"timeout={timeout}")
            if post_answer is not None:
                return post_answer()
            return _async("op-acl")

    class _Api:
        """client.api: subscriptable for "network-acls", attribute for operations."""
        operations = ops

        def __getitem__(self, key):
            return {"network-acls": _Acls()}[key]

    client = SimpleNamespace(api=_Api())
    svc = SimpleNamespace(connection_manager=SimpleNamespace(get_client=lambda host: client))
    monkeypatch.setattr(acls_module, "require_client", lambda c, host: c)
    monkeypatch.setattr(acls_module, "resolve_owner", lambda c, kind, project: "default")
    monkeypatch.setattr(acls_module, "project_is_visible", lambda host, project: True)
    monkeypatch.setattr(acls_module, "verify_cluster_access", lambda host: None, raising=False)
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
    return TestClient(app, raise_server_exceptions=False), calls, ops


def test_an_acl_create_waits_for_its_operation(monkeypatch) -> None:
    tc, calls, ops = _acl_app(monkeypatch)
    r = tc.post("/network-acls?host=nuc1", json={"name": "qaacl", "description": "", "ingress": [], "egress": [], "config": {}})
    assert r.status_code == 200, r.text
    assert calls == ["post", f"timeout={lxd_utils.write_timeout(acls_module.ACL_WRITE_WAIT_SECONDS)}"]
    assert [w["op"] for w in ops.waits] == ["op-acl"]


def test_a_bad_acl_name_is_refused_at_the_boundary_not_by_the_daemon(monkeypatch) -> None:
    """"has space" reached LXD, which refused it, and the refusal came back as a 500."""
    tc, calls, _ = _acl_app(monkeypatch)
    for bad in ("has space", "1abc", "../x", "a" * 64, ""):
        r = tc.post("/network-acls?host=nuc1", json={"name": bad, "ingress": [], "egress": []})
        assert r.status_code == 422, (bad, r.text)
    assert calls == [], "nothing was sent to the daemon"


def test_the_daemon_s_own_refusal_keeps_its_status(monkeypatch) -> None:
    def refused():
        raise pylxd_exceptions.LXDAPIException(_Response({"type": "error", "error": "Name already in use"}, 409))
    tc, calls, _ = _acl_app(monkeypatch, post_answer=refused)
    r = tc.post("/network-acls?host=nuc1", json={"name": "qaacl", "ingress": [], "egress": []})
    assert r.status_code == 409, r.text
    assert "Name already in use" in r.text


def test_an_elapsed_wait_slice_is_not_a_failure() -> None:
    """LXD answers an elapsed `?timeout=` with "context deadline exceeded" while the operation
    runs on. A NIC address write on a clustered OVN instance takes about six seconds, one
    second more than a slice, and was reported as refused while the cluster applied it."""
    ops = _Operations(settle_after=3, deadline_errors=True)
    client = SimpleNamespace(api=SimpleNamespace(operations=ops))
    lxd_utils.wait_for_lxd_operation(client, _async("op-slow"), timeout=30)
    assert [w["params"]["timeout"] for w in ops.waits] == [5, 5, 5]
    assert ops.reads == ["op-slow", "op-slow"], "each elapsed slice asks the operation itself"


def test_a_failure_after_an_elapsed_slice_still_surfaces() -> None:
    ops = _Operations(settle_after=2, final="Failure", deadline_errors=True)
    client = SimpleNamespace(api=SimpleNamespace(operations=ops))
    try:
        lxd_utils.wait_for_lxd_operation(client, _async("op-bad"), timeout=30)
    except pylxd_exceptions.LXDAPIException as exc:
        assert "boom" in str(exc)
    else:
        raise AssertionError("a failed operation must raise")


def test_our_own_deadline_names_the_operation_not_the_daemon() -> None:
    ops = _Operations(settle_after=99, deadline_errors=True)
    client = SimpleNamespace(api=SimpleNamespace(operations=ops))
    try:
        lxd_utils.wait_for_lxd_operation(client, _async("op-stuck"), timeout=12)
    except pylxd_exceptions.LXDAPIException as exc:
        assert "op-stuck" in str(exc) and "12s" in str(exc)
        assert "context deadline" not in str(exc)
    else:
        raise AssertionError("an operation still running at the deadline must raise")
    assert [w["params"]["timeout"] for w in ops.waits] == [5, 5, 2]


def test_an_operation_that_vanished_after_an_elapsed_slice_reports_the_wait_error() -> None:
    ops = _Operations(settle_after=99, deadline_errors=True, vanished=True)
    client = SimpleNamespace(api=SimpleNamespace(operations=ops))
    try:
        lxd_utils.wait_for_lxd_operation(client, _async("op-gone"), timeout=30)
    except pylxd_exceptions.LXDAPIException as exc:
        assert "context deadline exceeded" in str(exc)
    else:
        raise AssertionError("an unreadable operation must not pass as success")
    assert len(ops.waits) == 1


def test_a_synchronous_write_is_sent_with_a_read_timeout_that_outlives_it(monkeypatch) -> None:
    """LXD 5.21 finishes a network create before it answers. An OVN create on a three-member
    cluster ran past the pooled client's 8 s read, so the console reported "Failed to create
    network" while the daemon created it. The write carries its own read timeout."""
    client, nets, _ = _networks_app(monkeypatch, async_writes=False)
    r = client.post("/networks?host=nuc1", json=BRIDGE)
    assert r.status_code == 200, r.text
    assert nets.posts and all(p["timeout"] == lxd_utils.write_timeout(networks_module.NETWORK_WRITE_WAIT_SECONDS) for p in nets.posts)
    assert nets.posts[0]["timeout"][1] > 8, "the read must outlive the pooled client's 8 s"
    r = client.delete(f"/networks/{BRIDGE['name']}?host=nuc1")
    assert r.status_code == 200, r.text
    assert nets.delete_timeouts == [lxd_utils.write_timeout(networks_module.NETWORK_WRITE_WAIT_SECONDS)]
