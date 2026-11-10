"""A project quota is a cap, not a reservation, so an over-committed cluster still takes one.

cpu_available is threads minus the running instances' allocation, so it goes negative on an
over-committed cluster (307 vCPUs allocated on 80 threads gives -227) and cannot bound a cap.
LXD itself allows any limit and any over-commit; the only figure that makes a limit nonsensical
is the hardware total. The pre-flight refuses a cap above the hardware and names both numbers,
for CPU, memory and storage, on create and on update.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import projects as projects_module

GIB = 1024 ** 3

OVERCOMMITTED = {
    "cpu_total": 80,
    "cpu_used": 307,
    "cpu_available": 0,
    "memory_total": 256 * GIB,
    "memory_available": 3 * GIB,
    "storage_total": 4000 * GIB,
    "storage_available": 200 * GIB,
    "storage_pools": [],
}


class _Service:
    def __init__(self, resources):
        self._resources = resources
        self.created = []

    def get_node_resources(self, host):
        return dict(self._resources)

    async def create_project(self, **kwargs):
        self.created.append(kwargs)
        return {"name": kwargs["name"], "host": kwargs["host"], "description": kwargs.get("description") or "", "config": {}, "used_by": []}

    async def list_projects(self, host):
        return [{"name": "qa-quota", "host": host, "description": "", "config": {"limits.cpu": "4"}, "used_by": []}]

    async def update_project(self, *args, **kwargs):
        self.created.append(kwargs)
        return {"name": "qa-quota", "host": "mc-lab", "description": "", "config": {"limits.cpu": str(kwargs.get("max_cpu", 4))}, "used_by": []}


def _client(service, monkeypatch):
    from app.core.auth import AuthenticatedUser
    from app.core.database import get_db
    from app.services.lxd_service import get_lxd_service
    from app.core.rbac import get_current_user
    from app.core.org_scope import org_scope_dependency
    from app.core.dependencies import require_tenant, require_partner, check_read_only_mode

    monkeypatch.setattr(projects_module, "verify_cluster_access", lambda host: None)
    monkeypatch.setattr(projects_module.activity_logger, "log_activity", lambda **k: None, raising=False)

    class _Cache:
        def __getattr__(self, name):
            return lambda *a, **k: None

        def delete(self, *a, **k):
            return None

        def delete_pattern(self, *a, **k):
            return None

        def get(self, *a, **k):
            return None

        def set(self, *a, **k):
            return None

    monkeypatch.setattr(projects_module, "get_cache_manager", lambda: _Cache())
    app = FastAPI()
    app.include_router(projects_module.router)
    app.dependency_overrides[get_lxd_service] = lambda: service
    admin = AuthenticatedUser(
        "quota-admin",
        email="quota-admin@example.invalid",
        groups=["role_admin"],
    )
    app.dependency_overrides[get_current_user] = lambda: admin
    app.dependency_overrides[get_db] = lambda: object()
    app.dependency_overrides[org_scope_dependency] = lambda: None
    app.dependency_overrides[require_tenant()] = lambda: None
    app.dependency_overrides[require_partner()] = lambda: admin
    app.dependency_overrides[check_read_only_mode] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def _body(**overrides):
    body = {"host": "mc-lab", "name": "qa-quota", "max_cpu": 8, "max_ram": "8GiB", "max_storage": "50GiB"}
    body.update(overrides)
    return body


def test_a_cap_under_the_hardware_total_is_accepted_when_over_committed(monkeypatch):
    service = _Service(OVERCOMMITTED)
    r = _client(service, monkeypatch).post("/projects", json=_body(max_cpu=8))
    assert r.status_code == 201, r.text
    assert service.created and service.created[0]["max_cpu"] == 8


def test_a_cap_above_the_hardware_total_is_refused_naming_both_numbers(monkeypatch):
    r = _client(_Service(OVERCOMMITTED), monkeypatch).post("/projects", json=_body(max_cpu=81))
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "81" in detail and "80" in detail


def test_memory_and_storage_caps_compare_against_totals_not_free_space(monkeypatch):
    tc = _client(_Service(OVERCOMMITTED), monkeypatch)
    assert tc.post("/projects", json=_body(max_ram="64GiB")).status_code == 201, "3 GiB free, 256 total"
    assert tc.post("/projects", json=_body(max_ram="512GiB")).status_code == 400
    assert tc.post("/projects", json=_body(max_storage="1000GiB")).status_code == 201, "200 GiB free, 4000 total"
    assert tc.post("/projects", json=_body(max_storage="9000GiB")).status_code == 400


def test_the_update_path_holds_the_same_rule(monkeypatch):
    service = _Service(OVERCOMMITTED)
    tc = _client(service, monkeypatch)
    r = tc.patch("/projects/mc-lab/qa-quota", json={"max_cpu": 16})
    assert r.status_code == 200, r.text
    r = tc.patch("/projects/mc-lab/qa-quota", json={"max_cpu": 400})
    assert r.status_code == 400
    assert "80" in r.json()["detail"]
