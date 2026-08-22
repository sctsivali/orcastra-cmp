"""An unreachable cluster must answer 503. Never 500, never 400, never 404.

Observed on the live fleet with one dead cluster (blitz01), before this was fixed:

    GET  /api/v1/projects/blitz01                 500 x14
         "LXD operation error: Failed to list projects: No connection to host: blitz01.
          Client returned None."
    POST /api/v1/vms/blitz01/web01/start          400
    GET  /api/v1/vms/blitz01/web01/metrics        404  "Instance 'web01' not found on blitz01"
    GET  /api/v1/server/blitz01                   500

500 says the server is broken and pages an engineer for a cluster that is merely offline.
404 says the operator's instance was deleted. Monitoring, alerting, retry policy and SLO
accounting all read the status, so it is the contract, not a detail.

These drive the real handlers through a real app with a real service object, so the assertion
covers the whole path: the route, its dependencies, the guard ordering AND the except-clause
ordering. That last one is what makes this worth writing. Three separate defects in this sweep
were invisible to a structural check because the code raised the right thing and a broad
handler two lines later converted it:

  * projects.py raised inside a try whose `except Exception` re-wrapped it, so changing the
    exception type alone accomplished nothing.
  * eleven router guards raised HTTPException(503) into a try with no `except HTTPException:
    raise`, so the 503 became a 500.
  * changing instance.py's raise type without editing vms.py would have moved six endpoints
    from 400 to 500, which is worse than the bug being fixed.

Hermetic. `LXDService.__new__` skips `__init__`, so there is no Vault client, no pylxd and no
cluster sync. The connection manager is a stub whose `get_client` returns None, which is
exactly what the real breaker does for a tripped cluster. No database, no Redis, no network.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.lxd.lxd_service import LXDService
from app.services.lxd.projects import ProjectManager
from app.services.lxd.instance import InstanceManager
from app.services.lxd.resources import ResourceManager


HOST = "blitz01"


class _BreakerTrippedConnectionManager:
    """Stands in for a connection manager whose breaker has tripped.

    The real one returns None from get_client without making any network call once
    health.is_tripped(cluster) is true. Returning None is therefore not a simulation of an
    edge case, it is the ordinary steady state for any cluster that is down.
    """

    def get_client(self, host, *args, **kwargs):
        return None

    def get_node_names(self):
        return [HOST]

    def connect_to_node(self, *args, **kwargs):
        return None

    def sync_registered_clusters(self, *args, **kwargs):
        return None


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

    Only three dependencies are stubbed, and each for a reason that keeps the test hermetic
    without weakening what it asserts:

      get_current_user      -> None, which makes require_roles hand back a dev admin
                               (rbac.py). Authorization is not what this file is about, and a
                               real token would need Authentik.
      org_scope_dependency  -> a no-op. It queries Postgres for the caller's clusters.
      get_lxd_service       -> the real service with a breaker-tripped connection manager.

    Everything downstream of those is the production code path, which is the point: the defect
    lives in the ordering of except clauses, and only a real request exercises that.
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


# --- Tier A: the service layer raised the wrong exception type -----------------------------

def test_projects_read_is_503_not_500():
    """The observed defect: 14 x HTTP 500 on a read against a dead cluster."""
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
    """The worst status in the sweep.

    A None client reaches find_instance_in_projects, which swallows the AttributeError in both
    of its loops and returns (None, None). The caller then reports "Instance 'web01' not found
    on blitz01" and the router message-sniffs "not found" into a 404. That is a false statement
    about the operator's data, not merely a wrong severity.
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
    """The sibling of the route above, and it was missing both halves of the guard.

    lxd_service.get_node_resources called get_client without require_client, so an unreachable
    cluster arrived as a None dereference, and the route had no `except HTTPException: raise`
    to let a 503 past its broad handler. Fixing only one half accomplishes nothing, which is
    why this asserts through the real route rather than at the service.
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
    """The list endpoint used to return 200 with is_clustered=False for a dead cluster.

    The members page renders that as "Not a Clustered Node", so an outage read as a
    configuration fact: the operator is told their cluster is not a cluster. Its sibling
    detail handler has always answered 503 through require_client.
    """
    from app.routers import cluster_members

    r = _app(cluster_members).get("/cluster-members", params={"node": HOST})
    assert r.status_code != 200, (
        f"a dead cluster answered 200, body {r.text[:200]}"
    )
    _assert_unreachable(r, "GET /cluster-members")


def test_an_unreachable_cluster_is_not_logged_at_error():
    """Severity is a contract too, for the same reason the status code is.

    An offline cluster is expected operational noise, so anything alerting on ERROR pages
    for a host the operator already knows is down. The routers were moved to WARNING in an
    earlier commit, which left the volume untouched: the service layer logged every failed
    connect at ERROR, and on the dev fleet that one line produced most of 2383 ERROR
    records in 90 minutes.
    """
    import ast
    import pathlib

    app_dir = pathlib.Path(__file__).resolve().parents[1] / "app"
    # note: the first version of this list held only the two phrases the fix had already
    # touched, so it passed while two more sites kept logging the same thing in slightly
    # different words. The live run found them by counting, not by reading. Match on the
    # shape of the statement rather than on one spelling of it.
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
