"""GET /projects/{host}/{project}/usage says how much of each project limit is allocated.

The create-instance form had the project's limits (they ride raw in ProjectInfo.config) and
nothing about how much of them was already spent, so LXD's synchronous refusal was the first
and only signal. This read sums what LXD sums (lxd/project/limits.go): every instance in the
project whatever its state, its expanded config, so a profile-inherited limit counts; memory
through LXD's size suffixes; instance counts by type. A partial read is marked incomplete so
the form never says "4 of 4" on a floor.

Scoped like the list route: a project the caller may not see answers 404, and the body
carries aggregates only, never an instance name.
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.lxd.project_usage import ProjectUsage, parse_lxd_size, read_project_usage

GIB = 1024 ** 3


@pytest.mark.parametrize(
    "value, expected",
    [
        ("8GiB", 8 * GIB), ("8GB", 8 * 10**9), ("512MiB", 512 * 1024 ** 2), ("1TiB", 1024 ** 4),
        ("1024", 1024), ("2kB", 2000), ("2KiB", 2048), ("50%", None), ("", None), (None, None), ("junk", None),
    ],
)
def test_parse_lxd_size(value, expected):
    assert parse_lxd_size(value) == expected


class _Api:
    """client.api.projects[name].get() and the instances listing list_in_project reads."""

    def __init__(self, config, rows, fail_instances=False):
        self._config = config
        self._rows = rows
        self._fail = fail_instances

    def project(self):
        return SimpleNamespace(json=lambda: {"metadata": {"name": "web-tier", "config": self._config}})


def _rows():
    return [
        {"name": "a", "type": "virtual-machine", "status": "Running", "config": {},
         "expanded_config": {"limits.cpu": "2", "limits.memory": "2GiB"}},
        {"name": "b", "type": "virtual-machine", "status": "Stopped", "config": {"limits.cpu": "2"},
         "expanded_config": {"limits.cpu": "2", "limits.memory": "4GiB"}},
        {"name": "c", "type": "container", "status": "Running", "config": {},
         "expanded_config": {"limits.cpu": "0-3", "limits.memory": "50%"}},
    ]


def _usage(monkeypatch, config, rows=None, fail_instances=False):
    from app.services.lxd import project_usage as mod

    monkeypatch.setattr(mod, "_read_project_config", lambda client, project: dict(config))

    def _list(client, collection, project, recursion=1, path=None):
        if fail_instances:
            raise RuntimeError("read timed out")
        return list(rows if rows is not None else _rows())

    monkeypatch.setattr(mod, "list_in_project", _list)
    return read_project_usage(object(), "web-tier")


def test_usage_sums_expanded_limits_of_every_instance_regardless_of_state(monkeypatch):
    got = _usage(monkeypatch, {"limits.cpu": "8", "limits.memory": "8GiB", "limits.instances": "5"})
    assert got.limits["cpu"] == 8 and got.limits["memory_bytes"] == 8 * GIB and got.limits["instances"] == 5
    assert got.used["cpu"] == 8, "2 + 2 (stopped still counts) + 4 (the range 0-3, inherited)"
    assert got.used["memory_bytes"] == 6 * GIB, "a percentage limit is not a byte figure and is left out"
    assert got.used["instances"] == 3
    assert got.used["virtual_machines"] == 2 and got.used["containers"] == 1
    assert got.complete is True


def test_a_profile_inherited_limit_is_counted(monkeypatch):
    rows = [{"name": "x", "type": "container", "status": "Running", "config": {}, "expanded_config": {"limits.cpu": "6"}}]
    assert _usage(monkeypatch, {"limits.cpu": "8"}, rows).used["cpu"] == 6


def test_an_instance_without_a_limit_counts_zero_here(monkeypatch):
    """LXD refuses to create an instance without limits.cpu in a project that limits it, so a
    missing value is not "one core" the way the member read counts it; it is nothing."""
    rows = [{"name": "x", "type": "container", "status": "Running", "config": {}, "expanded_config": {}}]
    assert _usage(monkeypatch, {"limits.cpu": "8"}, rows).used["cpu"] == 0


def test_unset_limits_are_none_not_zero(monkeypatch):
    got = _usage(monkeypatch, {})
    assert got.limits == {"cpu": None, "memory_bytes": None, "instances": None, "containers": None,
                          "virtual_machines": None, "disk_bytes": None}


def test_a_partial_read_is_marked_incomplete(monkeypatch):
    got = _usage(monkeypatch, {"limits.cpu": "4"}, fail_instances=True)
    assert got.complete is False
    assert got.limits["cpu"] == 4
    assert got.used["cpu"] == 0


class _Service:
    def __init__(self, usage=None, error=None):
        self._usage = usage
        self._error = error
        self.calls = []

    async def get_project_usage(self, host, project):
        self.calls.append((host, project))
        if self._error:
            raise self._error
        return self._usage


class _Cache:
    def __init__(self):
        self.store = {}

    def get(self, ns, key):
        return self.store.get((ns, key))

    def set(self, ns, key, value, ttl=None):
        self.store[(ns, key)] = value

    def __getattr__(self, name):
        return lambda *a, **k: None


def _client(service, monkeypatch, cache=None, visible=True):
    from app.routers import projects as projects_module
    from app.services.lxd_service import get_lxd_service
    from app.core.rbac import get_current_user
    from app.core.org_scope import org_scope_dependency
    from app.core.dependencies import require_tenant

    monkeypatch.setattr(projects_module, "verify_cluster_access", lambda host: None)
    monkeypatch.setattr(projects_module, "project_is_visible", lambda host, project: visible)
    monkeypatch.setattr(projects_module, "filter_projects_by_acl", lambda host, names: names if visible else [])
    monkeypatch.setattr(projects_module, "get_cache_manager", lambda: cache or _Cache())
    app = FastAPI()
    app.include_router(projects_module.router)
    app.dependency_overrides[get_lxd_service] = lambda: service
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    app.dependency_overrides[require_tenant()] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def _sample():
    return ProjectUsage(
        limits={"cpu": 4, "memory_bytes": 8 * GIB, "instances": None, "containers": None, "virtual_machines": None, "disk_bytes": None},
        used={"cpu": 4, "memory_bytes": 6 * GIB, "instances": 1, "containers": 0, "virtual_machines": 1},
        complete=True,
    )


def test_the_route_answers_the_shape_the_form_reads(monkeypatch):
    r = _client(_Service(_sample()), monkeypatch).get("/projects/nuc1/web-tier/usage")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["host"] == "nuc1" and body["project"] == "web-tier"
    assert body["limits"]["cpu"] == 4 and body["used"]["cpu"] == 4
    assert body["complete"] is True and body["collected_at"]
    assert "name" not in str(body["used"]), "aggregates only"


def test_usage_is_404_outside_the_callers_project_scope(monkeypatch):
    service = _Service(_sample())
    r = _client(service, monkeypatch, visible=False).get("/projects/nuc1/web-tier/usage")
    assert r.status_code == 404
    assert service.calls == [], "no cluster read for a project the caller may not see"


def test_usage_is_cached_briefly_and_dropped_by_an_instance_change(monkeypatch):
    from app.core.cache import CacheManager, LRUCache

    cache = _Cache()
    service = _Service(_sample())
    tc = _client(service, monkeypatch, cache=cache)
    tc.get("/projects/nuc1/web-tier/usage")
    tc.get("/projects/nuc1/web-tier/usage")
    assert len(service.calls) == 1, "the second read is served from the 30 s cache"
    assert any(k[1].startswith("project_usage:host=nuc1:project=web-tier") for k in cache.store)

    real = CacheManager.__new__(CacheManager)
    real.redis_client = None
    real.memory_cache = LRUCache()
    real.default_ttl = 300
    real.sensitive_namespaces = []
    real.set("api_responses", "project_usage:host=nuc1:project=web-tier:scope=all", {"x": 1}, 30)
    real.invalidate_instance_cache()
    assert real.get("api_responses", "project_usage:host=nuc1:project=web-tier:scope=all") is None


def test_an_unreachable_cluster_is_503(monkeypatch):
    from app.services.lxd.exceptions import LXDConnectionError

    r = _client(_Service(error=LXDConnectionError("Could not connect to nuc1")), monkeypatch).get("/projects/nuc1/p/usage")
    assert r.status_code == 503
