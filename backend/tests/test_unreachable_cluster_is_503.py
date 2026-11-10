"""An unreachable cluster answers 503: never 500, 400 or 404.

500 says the server is broken and pages an engineer for a cluster that is only offline, 400 says
the request was bad, and 404 says the operator's instance was deleted. Monitoring, alerting, retry
policy and SLO accounting read the status, so it is part of the contract.

These drive the real handlers through a real app and service object, so they also cover
except-clause ordering: a broad `except Exception` converts the right exception unless an
`except HTTPException: raise` comes first. Hermetic: `LXDService.__new__` skips `__init__`, and
`get_client` returns None, as the real breaker does for a tripped cluster.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.lxd.lxd_service import LXDService
from app.services.lxd import health
from app.services.lxd.projects import ProjectManager
from app.services.lxd.instance import InstanceManager
from app.services.lxd.resources import ResourceManager


HOST = "bliss01"


class _BreakerTrippedConnectionManager:
    """Stands in for a connection manager whose breaker has tripped.

    The real one returns None from get_client without a network call once
    health.is_tripped(cluster) is true, the steady state for any cluster that is down.
    """

    def get_client(self, host, *args, **kwargs):
        return None

    def get_cached_client(self, host, *args, **kwargs):
        return None

    def get_node_names(self):
        return [HOST]

    def connect_to_node(self, *args, **kwargs):
        return None

    def sync_registered_clusters(self, *args, **kwargs):
        return None


@pytest.fixture(autouse=True)
def _trip_test_breaker():
    health.record_failure(HOST, "unreachable")
    try:
        yield
    finally:
        health.clear(HOST)


def _service() -> LXDService:
    svc = LXDService.__new__(LXDService)
    conn = _BreakerTrippedConnectionManager()
    svc.connection_manager = conn
    svc.resource_manager = ResourceManager()
    svc.cert_manager = None
    svc.project_manager = ProjectManager(conn, None)
    svc.instance_manager = InstanceManager(conn, svc.resource_manager, None)
    return svc


def _app(module) -> TestClient:
    """A minimal app carrying one real router and the real service object.

    Three dependencies are stubbed to keep the test hermetic:

      get_current_user      -> None, which makes require_roles hand back a dev admin
                               (rbac.py); a real token would need Authentik.
      org_scope_dependency  -> a no-op. It queries Postgres for the caller's clusters.
      get_lxd_service       -> the real service with a breaker-tripped connection manager.

    Everything downstream is the production code path, so a real request exercises the
    ordering of except clauses.
    """
    from app.services.lxd_service import get_lxd_service
    from app.core.rbac import get_current_user
    from app.core.org_scope import org_scope_dependency

    app = FastAPI()
    app.include_router(module.router)
    app.dependency_overrides[get_lxd_service] = _service
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def _assert_unreachable(response, label: str):
    assert response.status_code == 503, (
        f"{label}: an unreachable cluster must answer 503, got {response.status_code} "
        f"with body {response.text[:300]}"
    )
    detail = response.json().get("detail", "")
    assert HOST in str(detail), (
        f"{label}: the 503 must name the cluster so an operator knows which one is down, "
        f"got {detail!r}"
    )


# --- Tier A: the service layer raises an unreachable error ---------------------------------

def test_projects_read_is_503_not_500():
    """A project read against a dead cluster answers 503."""
    from app.routers import projects

    r = _app(projects).get(f"/projects/{HOST}")
    _assert_unreachable(r, "GET /projects/{host}")


def test_project_create_is_503_not_400():
    """400 says the operator sent a bad request. The request was fine; the cluster is down."""
    from app.routers import projects

    r = _app(projects).post(
        "/projects",
        json={
            "host": HOST,
            "name": "qa-probe",
            "max_cpu": 1,
            "max_ram": "1GiB",
            "max_storage": "1GiB",
        },
    )
    _assert_unreachable(r, "POST /projects")


def test_vm_get_is_503():
    from app.routers import vms

    r = _app(vms).get("/vms/web01", params={"host": HOST})
    _assert_unreachable(r, "GET /vms/{host}/{name}")


def test_vm_start_is_503_not_400():
    from app.routers import vms

    r = _app(vms).post("/vms/web01/start", json={"host": HOST})
    _assert_unreachable(r, "POST /vms/{host}/{name}/start")


def test_vm_stop_is_503_not_400():
    from app.routers import vms

    r = _app(vms).post("/vms/web01/stop", json={"host": HOST})
    _assert_unreachable(r, "POST /vms/{host}/{name}/stop")


def test_vm_metrics_does_not_claim_the_instance_is_missing():
    """A dead cluster must not read as a missing instance.

    A None client reaching find_instance_in_projects comes back as (None, None); the caller would
    then report "Instance 'web01' not found" and the router turns "not found" into a 404, a false
    statement about the operator's data.
    """
    from app.routers import vms

    r = _app(vms).get(f"/vms/{HOST}/web01/metrics")
    assert r.status_code != 404, (
        "a dead cluster must not be reported as a missing instance: "
        f"got 404 with body {r.text[:200]}"
    )
    _assert_unreachable(r, "GET /vms/{host}/{name}/metrics")


# --- Tier B: routers dereferencing a client the breaker may have refused --------------------

def test_server_info_is_503_not_500():
    from app.routers import lxd_server

    r = _app(lxd_server).get(f"/server/{HOST}")
    _assert_unreachable(r, "GET /server/{host}")


def test_server_resources_is_503_not_500():
    """Both halves of the guard are needed on this route.

    lxd_service.get_node_resources goes through require_client, so an unreachable cluster is a
    503 instead of a None dereference, and the route's `except HTTPException: raise` lets that
    503 past its broad handler. Asserting through the real route covers both.
    """
    from app.routers import lxd_server

    r = _app(lxd_server).get(f"/server/{HOST}/resources")
    _assert_unreachable(r, "GET /server/{host}/resources")


def test_operation_detail_does_not_claim_the_operation_is_missing():
    from app.routers import lxd_operations

    r = _app(lxd_operations).get("/operations/some-operation-id", params={"host": HOST})
    assert r.status_code != 404, (
        f"a dead cluster must not read as a missing operation, got {r.text[:200]}"
    )
    _assert_unreachable(r, "GET /operations/{host}/{id}")


def test_storage_volume_detail_does_not_claim_the_volume_is_missing():
    from app.routers import storage_volumes

    r = _app(storage_volumes).get("/storage/volumes/custom/vol1", params={"host": HOST, "pool": "default"})
    assert r.status_code != 404, (
        f"a dead cluster must not read as a missing volume, got {r.text[:200]}"
    )
    _assert_unreachable(r, "GET /storage/volumes/...")


def test_cluster_member_detail_does_not_claim_the_member_is_missing():
    from app.routers import cluster_members

    r = _app(cluster_members).get("/cluster-members/node01", params={"node": HOST})
    assert r.status_code != 404, (
        f"a dead cluster must not read as a missing member, got {r.text[:200]}"
    )
    _assert_unreachable(r, "GET /cluster-members/{host}/{name}")


def test_cluster_member_list_does_not_claim_the_node_is_standalone():
    """The list endpoint must not answer 200 with is_clustered=False for a dead cluster.

    The members page renders that as "Standalone server", so an outage would read as a
    configuration fact. The detail handler answers 503 through require_client.
    """
    from app.routers import cluster_members

    r = _app(cluster_members).get("/cluster-members", params={"node": HOST})
    assert r.status_code != 200, (
        f"a dead cluster answered 200, body {r.text[:200]}"
    )
    _assert_unreachable(r, "GET /cluster-members")


def test_an_unreachable_cluster_is_not_logged_at_error():
    """No failed connect to a cluster is logged at ERROR, in a router or the service layer.

    An offline cluster is expected operational noise, so an ERROR line pages anyone alerting
    on ERROR for a host the operator already knows is down.
    """
    import ast
    import pathlib

    app_dir = pathlib.Path(__file__).resolve().parents[1] / "app"
    # Several phrasings of the same message: one spelling would miss sites that word it
    # differently.
    phrases = (
        "Failed to connect to cluster",
        "Failed to connect cluster",
        "Could not connect to",
        "Connection failed for",
        "Connection error for",
    )

    offenders = []
    for path in sorted(app_dir.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != "error":
                continue
            if not (isinstance(node.func.value, ast.Name) and node.func.value.id == "logger"):
                continue
            if not node.args:
                continue
            text = ast.unparse(node.args[0])
            if any(p in text for p in phrases):
                offenders.append(
                    f"{path.relative_to(app_dir)}:{node.lineno} logs an unreachable "
                    "cluster at ERROR"
                )
    assert offenders == [], offenders
