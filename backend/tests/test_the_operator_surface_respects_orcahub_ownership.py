"""Orcastra's snapshot console must not damage the paid product running on the same instances.

OrcaHub sells snapshots and drives them through the same cluster. Three boundaries keep them apart:

* `pre-restore-` is OrcaHub's prefix, and its rolling prune deletes what matches. An Orcastra
  safety snapshot named that way would be removed without warning while the operator believes
  they have a rollback point.
* `snap-` is the pattern OrcaHub's LXD-native schedules generate. Reusing it makes an operator
  snapshot indistinguishable from a paid one.
* `snapshots.schedule` and `snapshots.expiry` implement a retention a tenant pays for. Writing
  them from here leaves OrcaHub's ledger disagreeing with the cluster.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.lxd import snapshots as snapshots_service
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


@pytest.mark.parametrize("reserved", ["pre-restore-something", "snap-20260908-0200"])
def test_a_reserved_name_is_refused(reserved):
    with pytest.raises(snapshots_service.SnapshotNameError):
        snapshots_service.assert_operator_snapshot_name(reserved)


def test_the_safety_snapshot_never_lands_in_orcahubs_prune_namespace():
    name = snapshots_service.build_safety_snapshot_name(INSTANCE)
    assert name.startswith("ops-"), name
    assert not name.startswith("pre-restore-"), (
        "OrcaHub's rolling prune deletes pre-restore-*; a safety snapshot named that way "
        "would be removed by another product"
    )


def test_a_generated_name_always_fits_lxds_limit():
    name = snapshots_service.build_operator_snapshot_name("x" * 200)
    assert len(name) <= 63, f"LXD refuses names over 63 characters, got {len(name)}"
    assert snapshots_service.assert_operator_snapshot_name(name) == name


def test_restore_never_writes_the_schedule_keys():
    # The instance carries a paid OrcaHub schedule. Nothing this router does may touch it.
    node = FakeInstanceNode(
        config={"snapshots.schedule": "@hourly", "snapshots.expiry": "1d"},
        snapshots=[{"name": "ops-target-20260908T000000Z"}],
    )
    response = _client(service(FakeClient(node))).post(
        f"/instances/{INSTANCE}/snapshots/ops-target-20260908T000000Z/restore",
        json={
            "host": HOST,
            "project": PROJECT,
            "confirm_name": INSTANCE,
            # The instance is managed, so the restore must pass the billing gate to reach the
            # behaviour under test.
            "reason": "verifying the schedule keys are never written",
            "acknowledge_billing_impact": True,
        },
    )
    assert response.status_code == 200, response.text

    for call in node.recorded:
        if call[0] == "put_instance":
            body = call[1]
            assert set(body) == {"restore"}, f"restore must send only a restore key, sent {body}"
            assert not any(k.startswith("snapshots.") for k in body), body


def test_every_action_on_a_managed_instance_is_refused_without_acknowledgement():
    """A source under a paid schedule is metered by the storage it occupies, so anything this
    console does there can pause a schedule a customer pays for. Default deny; proceeding is an
    explicit, attributable act.

    The refusal comes before the daemon is touched on every path: a restore refused after taking
    its safety snapshot would leave that snapshot on storage someone else is billed for.
    """
    managed = {"snapshots.schedule": "@hourly", "snapshots.expiry": "7d"}
    target = "ops-target-20260908T000000Z"

    node = FakeInstanceNode(config=managed)
    create = _client(service(FakeClient(node))).post(
        f"/instances/{INSTANCE}/snapshots",
        json={"host": HOST, "project": PROJECT, "label": "no acknowledgement"},
    )
    assert create.status_code == 403, create.text
    body = create.json()["detail"]
    assert body["error"] == "billing_impact_acknowledgement_required"
    assert body["schedule"] == "@hourly"
    assert body["acknowledge_field"] == "acknowledge_billing_impact"
    assert not [r for r in node.recorded if r[0] == "create_snapshot"]

    node = FakeInstanceNode(config=managed, snapshots=[{"name": target}])
    restore = _client(service(FakeClient(node))).post(
        f"/instances/{INSTANCE}/snapshots/{target}/restore",
        json={"host": HOST, "project": PROJECT, "confirm_name": INSTANCE},
    )
    assert restore.status_code == 403, restore.text
    assert not node.recorded, "a refused restore must not have taken a safety snapshot"

    node = FakeInstanceNode(config=managed, snapshots=[{"name": target}])
    delete = _client(service(FakeClient(node))).request(
        "DELETE",
        f"/instances/{INSTANCE}/snapshots/{target}",
        params={"host": HOST, "project": PROJECT},
    )
    assert delete.status_code == 403, delete.text
    assert not [r for r in node.recorded if r[0] == "delete_snapshot"]


def test_an_acknowledged_action_proceeds_and_is_audited_separately():
    """The override is one shot and carries its justification, and it gets its own high severity
    record beside the ordinary one so a forced action is alertable rather than one row among many.
    """
    node = FakeInstanceNode(config={"snapshots.schedule": "@hourly"})
    audited = []

    from app.core import snapshot_billing_gate

    original = snapshot_billing_gate.activity_logger.log_activity
    snapshot_billing_gate.activity_logger.log_activity = lambda **kw: audited.append(kw)
    try:
        response = _client(service(FakeClient(node))).post(
            f"/instances/{INSTANCE}/snapshots",
            json={
                "host": HOST,
                "project": PROJECT,
                "label": "emergency",
                "reason": "kernel upgrade rollback point",
                "acknowledge_billing_impact": True,
            },
        )
    finally:
        snapshot_billing_gate.activity_logger.log_activity = original

    assert response.status_code == 201, response.text
    assert [r for r in node.recorded if r[0] == "create_snapshot"]

    overrides = [r for r in audited if r["action"] == "instance.snapshot.billing_override"]
    ordinary = [r for r in audited if r["action"] == "instance.snapshot.create"]
    assert len(overrides) == 1, "exactly one override record, on its own action"
    assert ordinary, "the ordinary record is still written, so the two are separately queryable"
    assert overrides[0]["details"]["reason"] == "kernel upgrade rollback point"
    assert overrides[0]["details"]["overridden_action"] == "create"
    assert overrides[0]["details"]["external_schedule"] == "@hourly"


def test_an_acknowledgement_without_a_reason_is_still_refused():
    # The acknowledgement alone does not open the gate: the reason is what makes the audit row
    # attributable.
    node = FakeInstanceNode(config={"snapshots.schedule": "@hourly"})
    response = _client(service(FakeClient(node))).post(
        f"/instances/{INSTANCE}/snapshots",
        json={
            "host": HOST,
            "project": PROJECT,
            "label": "no words",
            "acknowledge_billing_impact": True,
        },
    )
    assert response.status_code == 403, response.text
    assert not node.recorded


def test_an_unmanaged_instance_needs_no_acknowledgement():
    node = FakeInstanceNode()
    response = _client(service(FakeClient(node))).post(
        f"/instances/{INSTANCE}/snapshots",
        json={"host": HOST, "project": PROJECT, "label": "ordinary"},
    )
    assert response.status_code == 201, response.text


def test_the_console_refuses_to_delete_an_upstream_snapshot_unacknowledged():
    # A tenant pays to keep these. Removing one needs an acknowledgement and a typed reason; this
    # request carries neither, so it is refused before the daemon is asked. A snapshot taken by
    # hand goes through a typed confirmation instead; see
    # test_a_snapshot_taken_on_the_host_is_the_operators_own.py.
    node = FakeInstanceNode(snapshots=[{"name": "snap-20260908-0200"}])
    response = _client(service(FakeClient(node))).request(
        "DELETE",
        f"/instances/{INSTANCE}/snapshots/snap-20260908-0200",
        params={"host": HOST, "project": PROJECT},
    )
    assert response.status_code == 403, response.text
    assert not [r for r in node.recorded if r[0] == "delete_snapshot"], (
        "a refused delete must not have reached the daemon"
    )


def test_the_policy_read_reports_external_management():
    node = FakeInstanceNode(config={"snapshots.schedule": "@daily", "snapshots.expiry": "7d"})
    response = _client(service(FakeClient(node))).get(
        f"/instances/{INSTANCE}/snapshots", params={"host": HOST, "project": PROJECT}
    )
    assert response.status_code == 200, response.text
    policy = response.json()["policy"]
    assert policy["externally_managed"] is True
    assert policy["schedule"] == "@daily"
    assert policy["expiry"] == "7d"
