"""A path parameter must not be able to change which resource the request addresses.

pylxd builds every URL by string concatenation with no escaping (`_APINode.__getitem__` at
pylxd/client.py:189-202). The snapshot routes put two caller-controlled segments into one path:

    /1.0/instances/{instance_name}/snapshots/{snapshot_name}

`instance_name` comes first, so a `?` or `#` in it pushes everything after it into the query
string or the fragment, and the daemon sees `DELETE /1.0/instances/{instance_name}`: an instance
delete. FastAPI hands the handler the percent-decoded value, so `%3F` on the wire arrives as a
literal `?`. On the read and restore paths the same truncation answers about a different resource.
"""

from urllib.parse import quote, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests._snapshot_fakes import HOST, PROJECT, FakeClient, FakeInstanceNode, service

# Names whose raw form ends the URL path early.
PATH_TRUNCATING = ["ct-victim?x=", "ct-victim#frag", "ct-victim?"]
# Names LXD itself would refuse, which must never reach it.
MALFORMED = ["ct victim", "ct/victim", "ct-" + "a" * 90, ""]


@pytest.fixture(autouse=True)
def _no_webhook(monkeypatch):
    from app.routers import instance_snapshots

    monkeypatch.setattr(instance_snapshots, "schedule_orcahub_event", lambda *a, **k: None)


def _client(svc) -> TestClient:
    from app.core.org_scope import org_scope_dependency
    from app.core.rbac import get_current_user
    from app.routers import instance_snapshots
    from app.services.lxd_service import get_lxd_service

    app = FastAPI()
    app.include_router(instance_snapshots.router)
    app.dependency_overrides[get_lxd_service] = lambda: svc
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def _built_path(instance_name: str, snapshot_name: str) -> str:
    """The path the daemon would receive, given pylxd's unescaped concatenation."""
    url = f"https://fake:8443/1.0/instances/{instance_name}/snapshots/{snapshot_name}"
    return urlsplit(url).path


def test_the_truncation_is_real_before_asserting_the_guard():
    """The hazard exists, so the guard tests below are meaningful."""
    assert _built_path("ct-victim?x=", "ops-x") == "/1.0/instances/ct-victim", (
        "a '?' in the instance name must be shown to truncate the path, or this file proves nothing"
    )
    assert _built_path("ct-ok", "ops-x") == "/1.0/instances/ct-ok/snapshots/ops-x"


@pytest.mark.parametrize("hostile", PATH_TRUNCATING + MALFORMED)
def test_delete_refuses_an_instance_name_that_could_rewrite_the_path(hostile):
    node = FakeInstanceNode(snapshots=[{"name": "ops-target-20260908T000000Z"}])
    client = _client(service(FakeClient(node)))
    response = client.delete(
        f"/instances/{quote(hostile, safe='')}/snapshots/ops-target-20260908T000000Z"
        f"?host={HOST}&project={PROJECT}"
    )
    assert response.status_code in (403, 404, 422), response.text
    assert not [c for c in node.recorded if c[0] == "delete_snapshot"], (
        f"'{hostile}' reached the daemon"
    )


@pytest.mark.parametrize("hostile", PATH_TRUNCATING)
def test_the_read_path_refuses_it_too(hostile):
    """A truncated GET would answer about the instance instead of its snapshots."""
    node = FakeInstanceNode()
    client = _client(service(FakeClient(node)))
    response = client.get(
        f"/instances/{quote(hostile, safe='')}/snapshots?host={HOST}&project={PROJECT}"
    )
    assert response.status_code in (403, 404, 422), response.text


@pytest.mark.parametrize("hostile", PATH_TRUNCATING)
def test_restore_refuses_it_too(hostile):
    node = FakeInstanceNode(snapshots=[{"name": "ops-target-20260908T000000Z"}])
    client = _client(service(FakeClient(node)))
    response = client.post(
        f"/instances/{quote(hostile, safe='')}/snapshots/ops-target-20260908T000000Z/restore",
        json={"host": HOST, "project": PROJECT, "confirm_name": hostile},
    )
    assert response.status_code in (403, 404, 422), response.text
    assert not [c for c in node.recorded if c[0] == "put_instance"], (
        f"'{hostile}' reached the daemon"
    )


def test_an_ordinary_instance_name_still_works():
    """The guard must not break the normal path."""
    node = FakeInstanceNode(snapshots=[{"name": "ops-target-20260908T000000Z"}])
    client = _client(service(FakeClient(node)))
    response = client.delete(
        f"/instances/ct-qa-fixture-0001/snapshots/ops-target-20260908T000000Z"
        f"?host={HOST}&project={PROJECT}"
    )
    assert response.status_code == 200, response.text
