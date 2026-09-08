"""Orcastra's snapshot console must not damage the paid product running on the same instances.

OrcaHub sells snapshots and drives them through this same cluster. Three boundaries keep the two
from corrupting each other, and each one is here because breaking it has a concrete cost:

* `pre-restore-` is OrcaHub's prefix and its rolling prune physically DELETES what matches. An
  Orcastra safety snapshot named that way would be removed by another product without warning,
  which is worse than not taking one, because the operator believes they have a rollback point.
* `snap-` is the pattern OrcaHub's LXD-native schedules generate. Reusing it makes an operator
  snapshot indistinguishable from a paid one in every surface that looks at the cluster.
* `snapshots.schedule` and `snapshots.expiry` are how OrcaHub implements a retention a tenant is
  paying for. Writing them from here leaves its ledger disagreeing with the cluster.
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
        json={"host": HOST, "project": PROJECT, "confirm_name": INSTANCE},
    )
    assert response.status_code == 200, response.text

    for call in node.recorded:
        if call[0] == "put_instance":
            body = call[1]
            assert set(body) == {"restore"}, f"restore must send only a restore key, sent {body}"
            assert not any(k.startswith("snapshots.") for k in body), body


def test_a_managed_instance_requires_a_reason_before_an_operator_snapshot():
    node = FakeInstanceNode(config={"snapshots.schedule": "@hourly"})
    response = _client(service(FakeClient(node))).post(
        f"/instances/{INSTANCE}/snapshots",
        json={"host": HOST, "project": PROJECT, "label": "no reason given"},
    )
    assert response.status_code == 422, response.text
    assert not [r for r in node.recorded if r[0] == "create_snapshot"]

    ok = _client(service(FakeClient(node))).post(
        f"/instances/{INSTANCE}/snapshots",
        json={
            "host": HOST,
            "project": PROJECT,
            "label": "emergency",
            "reason": "kernel upgrade rollback point",
        },
    )
    assert ok.status_code == 201, ok.text


def test_the_console_refuses_to_delete_a_snapshot_it_did_not_create():
    # A tenant pays to keep these. An operator removing one from this console would destroy a
    # restore point silently, and the deletion would look like OrcaHub's own doing.
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
