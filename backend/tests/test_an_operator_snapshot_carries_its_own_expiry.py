"""An operator snapshot is created with an expiry and an ops- name, and the daemon receives both.

The lifetime is the control that keeps this feature from filling a storage pool, and it is
expressed to LXD as `expires_at` precisely so LXD prunes it whether or not Orcastra is running.
A test that only asserted a 201 would pass just as happily against a create that sent no expiry
at all, which is the shape of defect this repository keeps shipping: the mechanism runs, the
effect never happens.

pandora's `dir` pool has 28.5 GB free and copies the whole instance per snapshot, so an unbounded
snapshot there is not a theoretical tidiness problem.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests._snapshot_fakes import HOST, INSTANCE, PROJECT, FakeClient, FakeInstanceNode, service


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


def test_the_daemon_receives_an_expiry_and_an_ops_name():
    node = FakeInstanceNode()
    response = _client(service(FakeClient(node))).post(
        f"/instances/{INSTANCE}/snapshots",
        json={"host": HOST, "project": PROJECT, "label": "before upgrade"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "succeeded"

    creates = [r for r in node.recorded if r[0] == "create_snapshot"]
    assert len(creates) == 1, "exactly one create should have reached the daemon"
    body = creates[0][1]
    assert body["name"].startswith("ops-"), f"name must be operator-scoped, got {body['name']}"
    assert body.get("expires_at"), "the create must carry an expiry or nothing will prune it"
    assert body["stateful"] is False

    params = creates[0][2]
    assert params.get("project") == PROJECT, "the create must be scoped to the caller's project"


def test_the_expiry_is_bounded_even_when_a_caller_asks_for_more():
    node = FakeInstanceNode()
    response = _client(service(FakeClient(node))).post(
        f"/instances/{INSTANCE}/snapshots",
        json={"host": HOST, "project": PROJECT, "label": "long", "expiry_days": 365},
    )
    # The schema refuses out-of-range rather than silently clamping, so the operator learns the
    # ceiling exists instead of believing they got a year.
    assert response.status_code == 422, response.text


def test_the_cap_refuses_the_sixth_operator_snapshot():
    from app.services.lxd.snapshots import MAX_OPS_SNAPSHOTS_PER_INSTANCE

    existing = [
        {"name": f"ops-held-{i}", "created_at": "2026-09-01T00:00:00Z"}
        for i in range(MAX_OPS_SNAPSHOTS_PER_INSTANCE)
    ]
    node = FakeInstanceNode(snapshots=existing)
    response = _client(service(FakeClient(node))).post(
        f"/instances/{INSTANCE}/snapshots",
        json={"host": HOST, "project": PROJECT, "label": "one too many"},
    )
    assert response.status_code == 409, response.text
    assert not [r for r in node.recorded if r[0] == "create_snapshot"], (
        "a refused create must not have reached the daemon"
    )


def test_snapshots_belonging_to_the_paid_product_do_not_count_toward_the_cap():
    # OrcaHub's scheduled snapshots are named snap-*. Counting them would let a tenant's paid
    # retention lock the operator out of taking a maintenance snapshot at all.
    existing = [{"name": f"snap-2026090{i}-0200"} for i in range(6)]
    node = FakeInstanceNode(snapshots=existing)
    response = _client(service(FakeClient(node))).post(
        f"/instances/{INSTANCE}/snapshots",
        json={"host": HOST, "project": PROJECT, "label": "still allowed"},
    )
    assert response.status_code == 201, response.text


def test_a_create_waits_for_the_operation_before_reporting_success():
    """201 must mean the snapshot exists, not that the daemon accepted the request.

    LXD's create is asynchronous. Answering before the operation settles makes the caller's very
    next read find nothing, which is how a working feature looks broken. It also makes "created"
    a claim nobody checked.
    """
    node = FakeInstanceNode()
    response = _client(service(FakeClient(node))).post(
        f"/instances/{INSTANCE}/snapshots",
        json={"host": HOST, "project": PROJECT, "label": "waits"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "succeeded"
    assert response.json()["message"].startswith("Snapshot ops-")
    assert "still being written" not in response.json()["message"]


def test_an_operation_that_fails_is_not_reported_as_created():
    node = FakeInstanceNode()
    node.operation_status = "Failure"
    node.operation_err = "storage pool is full"

    response = _client(service(FakeClient(node))).post(
        f"/instances/{INSTANCE}/snapshots",
        json={"host": HOST, "project": PROJECT, "label": "doomed"},
    )
    assert response.status_code == 502, response.text
    assert "storage pool is full" in response.text


def test_an_unsettled_operation_is_accepted_rather_than_claiming_completion():
    # A dir-backed pool copies the whole instance, which can outlast any sensible request budget.
    # That is neither success nor failure and must not be reported as either.
    node = FakeInstanceNode()
    node.operation_status = "Running"

    response = _client(service(FakeClient(node))).post(
        f"/instances/{INSTANCE}/snapshots",
        json={"host": HOST, "project": PROJECT, "label": "slow pool"},
    )
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "pending"
    assert response.json()["operation_id"], "a pending response must name its LXD operation"
    assert "still being written" in response.json()["message"]


def test_a_pending_create_is_audited_invalidated_and_not_announced(monkeypatch):
    """Accepted work is visible to operators, but must not become a completed webhook event."""
    from app.routers import instance_snapshots

    audited = []
    emitted = []
    invalidated = []

    class _Cache:
        def invalidate_resource_cache(self, resource):
            invalidated.append(resource)

    monkeypatch.setattr(
        instance_snapshots.activity_logger,
        "log_activity",
        lambda **kwargs: audited.append(kwargs),
    )
    monkeypatch.setattr(
        instance_snapshots,
        "schedule_orcahub_event",
        lambda **kwargs: emitted.append(kwargs),
    )
    monkeypatch.setattr(instance_snapshots, "get_cache_manager", lambda: _Cache())

    node = FakeInstanceNode()
    node.operation_status = "Running"
    response = _client(service(FakeClient(node))).post(
        f"/instances/{INSTANCE}/snapshots",
        json={"host": HOST, "project": PROJECT, "label": "slow but accepted"},
    )

    assert response.status_code == 202, response.text
    assert response.json()["status"] == "pending"
    creates = [row for row in audited if row["action"] == "instance.snapshot.create"]
    assert len(creates) == 1
    assert creates[0]["result"] == "accepted"
    assert creates[0]["details"]["status"] == "pending"
    assert emitted == [], "snapshot.created must assert existence, not mere acceptance"
    assert invalidated == ["snapshots_fleet"]


def test_a_completed_create_is_audited_and_announced(monkeypatch):
    from app.routers import instance_snapshots

    audited = []
    emitted = []
    monkeypatch.setattr(
        instance_snapshots.activity_logger,
        "log_activity",
        lambda **kwargs: audited.append(kwargs),
    )
    monkeypatch.setattr(
        instance_snapshots,
        "schedule_orcahub_event",
        lambda **kwargs: emitted.append(kwargs),
    )

    response = _client(service(FakeClient(FakeInstanceNode()))).post(
        f"/instances/{INSTANCE}/snapshots",
        json={"host": HOST, "project": PROJECT, "label": "confirmed"},
    )

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "succeeded"
    creates = [row for row in audited if row["action"] == "instance.snapshot.create"]
    assert len(creates) == 1
    assert creates[0]["result"] == "success"
    assert creates[0]["details"]["status"] == "succeeded"
    assert len(emitted) == 1
    assert emitted[0]["event_type"] == "snapshot.created"
    assert emitted[0]["name"] == response.json()["snapshot"]


def test_the_pending_response_contract_requires_an_operation_id():
    from pydantic import ValidationError

    from app.models.schemas import SnapshotCreateResponse

    with pytest.raises(ValidationError, match="operation_id is required"):
        SnapshotCreateResponse(
            success=True,
            instance_name=INSTANCE,
            snapshot="ops-pending-20260920T000000Z",
            operation_id=None,
            status="pending",
            message="Snapshot is still being written.",
        )


def test_a_failed_create_does_not_expose_an_upstream_url():
    node = FakeInstanceNode()
    node.operation_status = "Failure"
    node.operation_err = "copy failed at https://10.20.30.40:8443/1.0/operations/secret"

    response = _client(service(FakeClient(node))).post(
        f"/instances/{INSTANCE}/snapshots",
        json={"host": HOST, "project": PROJECT, "label": "sanitized failure"},
    )

    assert response.status_code == 502, response.text
    assert "10.20.30.40" not in response.text
    assert "/operations/secret" not in response.text
    assert "[redacted]" in response.text


def test_a_negative_size_is_reported_as_no_size():
    """LXD answers `size: -1` when it did not compute one, which the bulk listing never does.

    Passed through, the API carries a negative byte count and any consumer that only checks for
    null renders it as a real value. The fleet view exists so an operator can see which snapshot
    is worth deleting, so the size column is the one that must not lie.
    """
    from app.services.lxd.snapshots import _normalize

    assert _normalize({"name": "ops-x", "size": -1}, "inst")["size"] is None
    assert _normalize({"name": "ops-x", "size": 0}, "inst")["size"] == 0
    assert _normalize({"name": "ops-x", "size": 511281152}, "inst")["size"] == 511281152
    assert _normalize({"name": "ops-x"}, "inst")["size"] is None


def test_no_expiry_is_null_rather_than_the_zero_time():
    from app.services.lxd.snapshots import _normalize

    row = _normalize({"name": "snap-x", "expires_at": "0001-01-01T00:00:00Z"}, "inst")
    assert row["expires_at"] is None
    assert row["operator_managed"] is False


def test_the_request_bounds_match_the_service_constants():
    """The schema declares the expiry bounds as literals, and the service owns the real values.

    They are not imported across, because models/schemas.py loads early and pulling the LXD
    service package in from there inverts the dependency direction. That leaves them free to
    drift, so the agreement is asserted instead of assumed: raising the service's ceiling without
    the schema would keep refusing what the service now allows, silently.
    """
    from app.models.schemas import SnapshotCreateRequest
    from app.services.lxd import snapshots as svc

    field = SnapshotCreateRequest.model_fields["expiry_days"]
    bounds = {type(m).__name__: getattr(m, "ge", getattr(m, "le", None)) for m in field.metadata}
    assert field.default == svc.DEFAULT_EXPIRY_DAYS
    assert bounds.get("Le") == svc.MAX_EXPIRY_DAYS, (
        f"schema ceiling {bounds.get('Le')} does not match the service's {svc.MAX_EXPIRY_DAYS}"
    )
    assert bounds.get("Ge") == 1
