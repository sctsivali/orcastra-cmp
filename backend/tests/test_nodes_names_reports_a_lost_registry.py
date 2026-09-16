"""GET /nodes/names says when the registry it answers with is not what Vault said.

The body stays a bare list for the API-key clients of this deprecated route. What a lost
registry looks like is what changed: the last known fleet with `X-Orcastra-Registry-Readable:
false` and `Cache-Control: no-store`, or 503 when nothing is known. Never an empty list, which
is what emptied every picker on the create-instance form for the length of a Vault blip.

The read runs through run_blocking on the availability pool with its own budget, because the
LIST used to run synchronously inside this async route and a slow Vault held every request
on the worker. On expiry the process's last known copy answers.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import vault_status
from app.core.blocking import BlockingPoolFull, BlockingTimeout
from app.services.lxd_certs.storage import RegistryRead

FLEET = ["i48", "ebi-01"]


class _CertManager:
    def __init__(self, read):
        self._read = read
        self.calls = 0

    def read_registry(self, use_cache=True):
        self.calls += 1
        if isinstance(self._read, Exception):
            raise self._read
        return self._read


class _Service:
    def __init__(self, read):
        self.cert_manager = _CertManager(read)

    def get_node_names(self):
        raise AssertionError("the route must read the registry off the loop, not through get_node_names")


def _app(read, monkeypatch, run_blocking=None):
    from app.routers import nodes as nodes_module
    from app.services.lxd_service import get_lxd_service
    from app.core.rbac import get_current_user
    from app.core.org_scope import org_scope_dependency

    if run_blocking is not None:
        monkeypatch.setattr(nodes_module, "run_blocking", run_blocking)
    app = FastAPI()
    app.include_router(nodes_module.router)
    service = _Service(read)
    app.dependency_overrides[get_lxd_service] = lambda: service
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    return TestClient(app, raise_server_exceptions=False), service


@pytest.fixture(autouse=True)
def _clean():
    vault_status.reset()
    yield
    vault_status.reset()


def test_a_readable_registry_answers_the_plain_list(monkeypatch):
    tc, _ = _app(RegistryRead(cluster_ids=list(FLEET), source="vault", readable=True), monkeypatch)
    r = tc.get("/nodes/names")
    assert r.status_code == 200
    assert r.json() == FLEET
    assert "x-orcastra-registry-readable" not in r.headers


def test_a_last_known_answer_carries_the_degraded_header_and_no_store(monkeypatch):
    tc, _ = _app(RegistryRead(cluster_ids=list(FLEET), source="last_known", readable=False), monkeypatch)
    r = tc.get("/nodes/names")
    assert r.status_code == 200
    assert r.json() == FLEET, "the fleet the operator had a minute ago, not an empty picker"
    assert r.headers["x-orcastra-registry-readable"] == "false"
    assert r.headers["cache-control"] == "no-store"


def test_nothing_known_is_503_not_an_empty_list(monkeypatch):
    tc, _ = _app(RegistryRead(cluster_ids=[], source=None, readable=False), monkeypatch)
    r = tc.get("/nodes/names")
    assert r.status_code == 503
    assert "registry" in r.json()["detail"].lower()
    assert r.headers["x-orcastra-registry-readable"] == "false"


def test_a_readable_empty_registry_is_a_plain_empty_list(monkeypatch):
    """A fresh install has no clusters. That is an answer, not a fault."""
    tc, _ = _app(RegistryRead(cluster_ids=[], source="vault", readable=True), monkeypatch)
    r = tc.get("/nodes/names")
    assert r.status_code == 200 and r.json() == []
    assert "x-orcastra-registry-readable" not in r.headers


def test_the_registry_read_runs_through_run_blocking_on_the_availability_pool(monkeypatch):
    seen = {}

    async def _fake_run_blocking(fn, *args, timeout, label, executor, max_in_flight=None, **kwargs):
        seen.update(timeout=timeout, label=label, max_in_flight=max_in_flight, fn=fn)
        return fn(*args, **kwargs)

    tc, service = _app(
        RegistryRead(cluster_ids=list(FLEET), source="vault", readable=True), monkeypatch, _fake_run_blocking
    )
    assert tc.get("/nodes/names").status_code == 200
    assert seen["label"] == "nodes.registry"
    assert seen["fn"] == service.cert_manager.read_registry
    assert seen["max_in_flight"] == 4, "a full pool must raise BlockingPoolFull, not queue forever"
    assert seen["timeout"] >= 12


def test_a_read_that_outlives_its_budget_is_answered_from_the_process_copy(monkeypatch):
    async def _expired(fn, *args, **kwargs):
        raise BlockingTimeout("nodes.registry", 12.0)

    vault_status.remember_registry(FLEET)
    tc, service = _app(RegistryRead(cluster_ids=list(FLEET), source="vault", readable=True), monkeypatch, _expired)
    r = tc.get("/nodes/names")
    assert r.status_code == 200 and r.json() == FLEET
    assert r.headers["x-orcastra-registry-readable"] == "false"


def test_a_full_pool_with_nothing_known_is_503(monkeypatch):
    async def _full(fn, *args, **kwargs):
        raise BlockingPoolFull("nodes.registry", 4)

    tc, _ = _app(RegistryRead(cluster_ids=list(FLEET), source="vault", readable=True), monkeypatch, _full)
    assert tc.get("/nodes/names").status_code == 503


def test_the_answer_is_filtered_on_the_loop_not_in_the_thread(monkeypatch):
    """The org-scope filter reads a ContextVar that does not travel into the executor thread,
    so an unfiltered read must be narrowed after it returns, on the loop."""
    from app.routers import nodes as nodes_module

    monkeypatch.setattr(nodes_module, "apply_cluster_filter", lambda ids: [i for i in ids if i == "i48"])
    tc, _ = _app(RegistryRead(cluster_ids=list(FLEET), source="vault", readable=True), monkeypatch)
    assert tc.get("/nodes/names").json() == ["i48"]
