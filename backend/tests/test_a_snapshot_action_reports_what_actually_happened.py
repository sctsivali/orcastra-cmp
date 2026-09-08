"""Delete and restore must not report an outcome the cluster did not produce.

Create already waited for its LXD operation and refused to answer 201 when the daemon rejected
the snapshot after accepting the request. Delete awaited the same wait and threw the result away,
then answered `success: true, "Snapshot X deleted."` regardless; restore never waited at all.
Both are the defect this feature was written to avoid, reintroduced two handlers later.

Also covered here: the ownership rule that delete enforced and restore did not, and the fact that
being refused on a billing-linked instance now leaves an audit record. A refusal that nothing
records is the event most worth alerting on and the one that was invisible.
"""

from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests._snapshot_fakes import HOST, INSTANCE, PROJECT, FakeClient, FakeInstanceNode, service

TARGET = "ops-target-20260908T000000Z"
FOREIGN = "snap-20260908-0130"


@pytest.fixture(autouse=True)
def _no_webhook(monkeypatch):
    from app.routers import instance_snapshots

    monkeypatch.setattr(instance_snapshots, "schedule_orcahub_event", lambda *a, **k: None)


@pytest.fixture
def audited(monkeypatch):
    from app.routers import instance_snapshots

    rows = []
    monkeypatch.setattr(
        instance_snapshots.activity_logger, "log_activity", lambda **kw: rows.append(kw)
    )
    return rows


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


def _restore(node, name=TARGET, **overrides):
    body = {"host": HOST, "project": PROJECT, "confirm_name": INSTANCE}
    body.update(overrides)
    return _client(service(FakeClient(node))).post(
        f"/instances/{INSTANCE}/snapshots/{name}/restore", json=body
    )


def _delete(node, name=TARGET, **params):
    # The name is percent-encoded the way a real client sends it, so a hostile one actually
    # reaches the handler instead of being mangled into a malformed URL by the test itself.
    query = f"host={HOST}&project={PROJECT}"
    for key, value in params.items():
        query += f"&{key}={value}"
    return _client(service(FakeClient(node))).delete(
        f"/instances/{INSTANCE}/snapshots/{quote(name, safe='')}?{query}"
    )


# ── the operation actually settled ─────────────────────────────────────────────────────────


def test_a_delete_the_daemon_rejects_is_not_reported_as_deleted():
    node = FakeInstanceNode(snapshots=[{"name": TARGET}])
    node.operation_status = "Failure"
    node.operation_err = "snapshot is in use"

    response = _delete(node)
    assert response.status_code == 502, response.text
    assert "snapshot is in use" in response.text


def test_a_delete_that_has_not_settled_says_so_rather_than_claiming_completion():
    node = FakeInstanceNode(snapshots=[{"name": TARGET}])
    node.operation_status = "Running"

    response = _delete(node)
    assert response.status_code == 200, response.text
    assert "is being removed" in response.json()["message"]


def test_a_restore_the_daemon_rejects_is_not_reported_as_started():
    node = FakeInstanceNode(snapshots=[{"name": TARGET}])
    node.operation_status = "Failure"
    node.operation_err = "instance is locked"

    response = _restore(node)
    assert response.status_code == 502, response.text
    body = response.json()["detail"]
    assert "instance is locked" in body
    # The safety copy is real and still on the pool, so the operator is told where it went.
    assert "ops-pre-" in body, body


def test_a_settled_restore_says_it_finished():
    node = FakeInstanceNode(snapshots=[{"name": TARGET}])
    response = _restore(node)
    assert response.status_code == 200, response.text
    assert "restored to" in response.json()["message"]


# ── ownership, on both destructive actions ─────────────────────────────────────────────────


def test_restore_refuses_a_snapshot_this_console_did_not_create():
    node = FakeInstanceNode(snapshots=[{"name": FOREIGN}])
    response = _restore(node, name=FOREIGN)
    assert response.status_code == 403, response.text
    assert not node.recorded, "a refused restore must not reach the daemon at all"


@pytest.mark.parametrize(
    "hostile",
    [
        "ops-x/../../instances/victim",
        "ops-x%2f..%2fvictim",
        "ops-" + "a" * 90,
        "ops-x?project=other",
    ],
)
def test_a_name_that_is_not_a_plain_snapshot_name_never_reaches_a_request_path(hostile):
    """pylxd interpolates this straight into the LXD URL with no escaping."""
    node = FakeInstanceNode(snapshots=[{"name": TARGET}])
    response = _delete(node, name=hostile)
    assert response.status_code in (403, 404), response.text
    assert not [c for c in node.recorded if c[0] == "delete_snapshot"], (
        "the name reached the daemon"
    )


def test_a_restore_of_a_snapshot_that_is_not_there_leaves_no_safety_copy_behind():
    """The safety copy used to be taken first, so a typo cost a snapshot on a billed source."""
    node = FakeInstanceNode(snapshots=[{"name": "ops-something-else-20260908T000000Z"}])
    response = _restore(node)
    assert response.status_code == 404, response.text
    assert not [c for c in node.recorded if c[0] == "create_snapshot"], (
        "nothing may be written for a restore that cannot run"
    )


# ── a refusal is recorded ──────────────────────────────────────────────────────────────────


def test_being_refused_on_a_billed_instance_is_audited(audited):
    node = FakeInstanceNode(config={"snapshots.schedule": "@hourly"}, snapshots=[{"name": TARGET}])

    response = _delete(node)
    assert response.status_code == 403, response.text

    denied = [r for r in audited if r.get("result") == "denied"]
    assert denied, f"a refusal must leave a trace: {audited}"
    assert denied[0]["action"] == "instance.snapshot.delete"
    assert denied[0]["error_message"] == "billing_impact_acknowledgement_required"
    assert denied[0]["details"]["external_schedule"] == "@hourly"


def test_the_gate_runs_outside_the_handlers_try_so_a_refusal_is_not_a_500():
    """A 403 raised inside the try is swallowed by the generic except and re-raised as a 500."""
    node = FakeInstanceNode(config={"snapshots.schedule": "@hourly"}, snapshots=[{"name": TARGET}])
    assert _restore(node).status_code == 403
    assert _delete(node).status_code == 403
    assert not [c for c in node.recorded if c[0] in ("create_snapshot", "put_instance")], (
        "a refused action must not have written anything"
    )
