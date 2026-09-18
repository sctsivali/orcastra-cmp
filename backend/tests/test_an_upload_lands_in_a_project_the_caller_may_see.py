"""The dashboard upload takes a project, so the project has to be a real name, and the store
it lands in has to be one the caller may see. The import itself was already project-aware;
the dialog just never asked.
"""
import contextvars
import os

from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app.core import org_scope  # noqa: E402
from app.core.database import get_db  # noqa: E402
from app.core.dependencies import check_read_only_mode  # noqa: E402
from app.core.rbac import get_current_user  # noqa: E402
from app.routers import image_uploads_dashboard as dashboard  # noqa: E402
from app.services.lxd import get_lxd_service  # noqa: E402

CLUSTER = "nuc2"

# Which store each project reads: an own store is its own name, a shared one is default.
STORES = {"cust-other": "cust-other", "qa-shared": "default"}


class _Service:
    def image_store_of(self, host, project):
        return STORES.get(project, project)


def _client(visible: dict) -> TestClient:
    app = FastAPI()
    app.include_router(dashboard.router)

    async def _no_db():
        # Resolved before the handler runs, so it cannot refuse; a handler that reaches it
        # anyway fails on the first attribute, which is the right outcome for these cases.
        yield object()

    app.dependency_overrides[get_db] = _no_db
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope.org_scope_dependency] = lambda: None
    app.dependency_overrides[check_read_only_mode] = lambda: None
    app.dependency_overrides[get_lxd_service] = lambda: _Service()
    return TestClient(app, raise_server_exceptions=False)


def _post(client, project):
    def run():
        org_scope._visible_projects_var.set({CLUSTER: {"default", "qa-cap"}})
        org_scope._allowed_clusters_var.set(None)
        return client.post(
            dashboard.router.prefix or "/image-uploads",
            json={
                "cluster_id": CLUSTER, "project": project, "alias": "qa/probe",
                "instance_type": "container", "total_bytes": 10, "max_bytes": 10,
            },
        )

    return contextvars.copy_context().run(run)


def test_a_project_name_that_could_reshape_the_request_is_refused():
    r = _post(_client({}), "../x")
    assert r.status_code == 422, r.text
    assert "project" in r.json()["detail"].lower()


def test_a_project_outside_the_callers_bound_is_refused():
    # An own store the caller cannot see.
    r = _post(_client({}), "cust-other")
    assert r.status_code == 403, r.text


def test_a_shared_store_project_outside_the_bound_still_lands_in_the_default_store():
    # The project is outside the bound, but it keeps no store of its own: the upload lands in
    # the default store, which the caller may see, so the bound does not refuse it. The
    # request gets past both refusals; what it hits after that is the stand-in database.
    r = _post(_client({}), "qa-shared")
    assert r.status_code not in (403, 422), r.text
