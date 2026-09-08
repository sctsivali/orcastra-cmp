"""A project-level snapshot read must not hand a tenant instances they hold no grant on.

`check_tenant_permission` merges permissions from every rule matching the project when it is
given no instance name, which is correct for a project-scoped question and wrong as the only
check on a listing. A tenant holding `snapshot` on one instance passes it, and the fleet read
then answers with every snapshot of every instance in the project: names, sizes, ages, and the
source's running state for workloads that are not theirs.

The per-instance routes are unaffected, because they pass the instance name and the merge never
happens. This is specific to the shape of the fleet read.

Admin and Partner carry no ACL at all, so nothing here narrows what an operator sees.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests._snapshot_fakes import HOST, PROJECT, FakeClient, FakeInstanceNode, service

MINE = "ct-mine-0001"
THEIRS = "ct-theirs-0002"

# The grant that used to be enough to read the whole project.
ONE_INSTANCE_ACL = {HOST: {(PROJECT, MINE): {"view", "snapshot"}}}
WILDCARD_ACL = {HOST: {(PROJECT, "*"): {"view", "snapshot"}}}


def _listing():
    return [
        {
            "name": MINE,
            "status": "Running",
            "config": {},
            "snapshots": [{"name": "ops-mine-20260908T000000Z", "size": 10}],
        },
        {
            "name": THEIRS,
            "status": "Running",
            "config": {"snapshots.schedule": "@daily"},
            "snapshots": [{"name": "snap-20260908-0130", "size": 20}],
        },
    ]


def _client(svc, *, acl, flag_on: bool = True) -> TestClient:
    from app.core import org_scope
    from app.core.config import get_settings
    from app.core.org_scope import org_scope_dependency
    from app.core.rbac import get_current_user
    from app.routers import snapshots_fleet
    from app.services.lxd_service import get_lxd_service

    app = FastAPI()
    app.include_router(snapshots_fleet.router)
    app.dependency_overrides[get_lxd_service] = lambda: svc
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None

    settings = get_settings()
    original = settings.snapshot_tenant_enforcement_enabled
    object.__setattr__(settings, "snapshot_tenant_enforcement_enabled", flag_on)
    token = org_scope._tenant_acl_var.set(acl)

    client = TestClient(app, raise_server_exceptions=False)
    client._cleanup = lambda: (
        org_scope._tenant_acl_var.reset(token),
        object.__setattr__(settings, "snapshot_tenant_enforcement_enabled", original),
    )
    return client


def _fleet(client):
    return client.get(f"/snapshots?host={HOST}&project={PROJECT}")


@pytest.fixture
def fake():
    node = FakeInstanceNode()
    client = FakeClient(node)
    client.api.instances.project_listing = _listing()
    return client


def test_an_instance_scoped_grant_does_not_read_the_whole_project(fake):
    client = _client(service(fake), acl=ONE_INSTANCE_ACL)
    try:
        response = _fleet(client)
    finally:
        client._cleanup()

    assert response.status_code == 200, response.text
    body = response.json()
    sources = {row["source_name"] for row in body["items"]}
    assert sources == {MINE}, f"a foreign instance leaked into the listing: {sources}"
    assert body["total"] == len(body["items"]), "total must agree with the rows returned"


def test_a_wildcard_grant_still_sees_every_instance_in_the_project(fake):
    client = _client(service(fake), acl=WILDCARD_ACL)
    try:
        response = _fleet(client)
    finally:
        client._cleanup()

    assert response.status_code == 200, response.text
    sources = {row["source_name"] for row in response.json()["items"]}
    assert sources == {MINE, THEIRS}, "a wildcard grant must not be narrowed by the filter"


def test_an_operator_carries_no_acl_and_sees_the_whole_project(fake):
    # acl=None is what Admin and Partner actually have. The filter must be a no-op for them.
    client = _client(service(fake), acl=None, flag_on=False)
    try:
        response = _fleet(client)
    finally:
        client._cleanup()

    assert response.status_code == 200, response.text
    sources = {row["source_name"] for row in response.json()["items"]}
    assert sources == {MINE, THEIRS}, "an operator read must not be filtered"


def test_the_listing_carries_the_sources_schedule_so_the_gate_can_be_satisfied(fake):
    """Without this the Snapshots page cannot offer the acknowledgement the backend demands."""
    client = _client(service(fake), acl=None, flag_on=False)
    try:
        response = _fleet(client)
    finally:
        client._cleanup()

    rows = {row["source_name"]: row for row in response.json()["items"]}
    assert rows[THEIRS]["source_externally_managed"] is True
    assert rows[THEIRS]["source_schedule"] == "@daily"
    assert rows[MINE]["source_externally_managed"] is False
