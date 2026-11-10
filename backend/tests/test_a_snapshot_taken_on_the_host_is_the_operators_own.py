"""A snapshot taken by hand on the host is the operator's own.

Snapshots fall into three classes with three gates: straight through for one this console made
(`ops-`), a typed confirmation for one taken by hand on the host, and an acknowledgement plus a
reason for one the upstream snapshot product owns. Every test names the ablation that turns it
red.
"""

from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests._snapshot_fakes import (
    HOST,
    INSTANCE,
    PROJECT,
    FakeClient,
    FakeInstanceNode,
    service,
)

OURS = "ops-target-20260908T000000Z"
# Realistic hand-taken names. `pre-app-...` starts with `pre-` yet is not `pre-restore-`, which
# is the distinction under test.
MANUAL = "pre-app-upgrade-20260901T134000Z"
OTHER_MANUAL = "c35-db-pre-20260906T050115Z"
UPSTREAM = "snap-20260908-0130"


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


def _delete(node, name, **params):
    query = f"host={HOST}&project={PROJECT}"
    for key, value in params.items():
        query += f"&{key}={quote(str(value), safe='')}"
    return _client(service(FakeClient(node))).delete(
        f"/instances/{INSTANCE}/snapshots/{quote(name, safe='')}?{query}"
    )


def _restore(node, name, **overrides):
    body = {"host": HOST, "project": PROJECT, "confirm_name": INSTANCE}
    body.update(overrides)
    return _client(service(FakeClient(node))).post(
        f"/instances/{INSTANCE}/snapshots/{quote(name, safe='')}/restore", json=body
    )


def _touched(node, call):
    return [row for row in node.recorded if row[0] == call]


# ── classification ─────────────────────────────────────────────────────────────────────────


def test_the_row_says_where_the_snapshot_came_from():
    """Ablation: drop `origin` from _normalize, or let it disagree with operator_managed."""
    from app.services.lxd.snapshots import _normalize

    assert _normalize({"name": OURS}, INSTANCE)["origin"] == "operator"
    assert _normalize({"name": MANUAL}, INSTANCE)["origin"] == "manual"
    assert _normalize({"name": UPSTREAM}, INSTANCE)["origin"] == "upstream"
    assert _normalize({"name": "pre-restore-x"}, INSTANCE)["origin"] == "upstream"

    # `operator_managed` answers the narrower question: did this console make it.
    for name in (MANUAL, UPSTREAM):
        row = _normalize({"name": name}, INSTANCE)
        assert row["operator_managed"] is False
    assert _normalize({"name": OURS}, INSTANCE)["operator_managed"] is True


def test_the_snapshot_row_model_carries_every_field_the_service_stamps():
    """Ablation: add a field to _normalize and forget SnapshotInfo.

    A typed response model silently drops every field it does not declare.
    """
    from app.models.schemas import SnapshotInfo
    from app.services.lxd.snapshots import _normalize

    row = _normalize({"name": MANUAL, "size": 10}, INSTANCE)
    kept = SnapshotInfo(**row).model_dump()
    missing = sorted(set(row) - set(kept))
    assert missing == [], f"the response model drops what the service stamps: {missing}"


def test_a_hand_taken_snapshot_does_not_consume_the_operator_cap():
    """Ablation: widen count_operator_snapshots to the hand-taken or upstream classes.

    The cap counts only snapshots this console made, so manual restore points never spend an
    operator's budget of five.
    """
    from app.services.lxd.snapshots import count_operator_snapshots

    node = FakeInstanceNode(
        snapshots=[{"name": OURS}, {"name": MANUAL}, {"name": OTHER_MANUAL}, {"name": UPSTREAM}]
    )
    assert count_operator_snapshots(FakeClient(node), INSTANCE, PROJECT) == 1


# ── the hand-taken class ───────────────────────────────────────────────────────────────────


def test_a_hand_taken_snapshot_is_deletable_once_its_name_is_typed():
    """Ablation: refuse every name that does not start with `ops-`."""
    node = FakeInstanceNode(snapshots=[{"name": MANUAL}])
    response = _delete(node, MANUAL, confirm_snapshot=MANUAL)
    assert response.status_code == 200, response.text
    assert _touched(node, "delete_snapshot"), "the daemon must have been asked"


def test_a_hand_taken_snapshot_is_not_deleted_without_the_typed_name():
    """Ablation: let MANUAL fall through the gate.

    The recorded-calls assertion catches a 403 that arrives after the daemon was already asked.
    """
    node = FakeInstanceNode(snapshots=[{"name": MANUAL}])
    response = _delete(node, MANUAL)
    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "snapshot_confirmation_required"
    assert detail["confirm_field"] == "confirm_snapshot"
    assert detail["confirm_value"] == MANUAL
    assert not _touched(node, "delete_snapshot")


def test_a_confirmation_naming_a_different_snapshot_is_not_a_confirmation():
    """Ablation: compare truthiness instead of equality, or compare against the raw path value."""
    node = FakeInstanceNode(snapshots=[{"name": MANUAL}, {"name": OTHER_MANUAL}])
    response = _delete(node, MANUAL, confirm_snapshot=OTHER_MANUAL)
    assert response.status_code == 403, response.text
    assert not _touched(node, "delete_snapshot")


def test_a_hand_taken_restore_is_confirmed_by_the_instance_name_it_already_asks_for():
    """Restore asks for no second typed string: two typed fields in one dialog invite copy-paste.

    Ablation: make the gate demand `confirm_snapshot` on restore too.
    """
    node = FakeInstanceNode(snapshots=[{"name": MANUAL}])
    response = _restore(node, MANUAL)
    assert response.status_code == 200, response.text

    # The instance name is still required.
    mismatch = _restore(FakeInstanceNode(snapshots=[{"name": MANUAL}]), MANUAL, confirm_name="wrong")
    assert mismatch.status_code == 422


# ── the upstream class ─────────────────────────────────────────────────────────────────────


def test_an_upstream_snapshot_needs_both_an_acknowledgement_and_a_reason():
    """Ablation: accept the tick without the reason, mirroring the billing gate's own rule."""
    def attempt(**extra):
        node = FakeInstanceNode(snapshots=[{"name": UPSTREAM}])
        return node, _restore(node, UPSTREAM, **extra)

    node, refused = attempt()
    assert refused.status_code == 403
    assert refused.json()["detail"]["error"] == "upstream_snapshot_acknowledgement_required"
    assert not _touched(node, "create_snapshot"), "no safety copy on a refused restore"

    _node, tick_only = attempt(acknowledge_upstream_snapshot=True)
    assert tick_only.status_code == 403

    _node, words_only = attempt(reason="rolling back the failed upgrade")
    assert words_only.status_code == 403

    _node, allowed = attempt(
        acknowledge_upstream_snapshot=True, reason="rolling back the failed upgrade"
    )
    assert allowed.status_code == 200, allowed.text


def test_acknowledging_the_bill_does_not_acknowledge_the_owner():
    """The ownership gate reads its own field; the billing acknowledgement does not open it.

    A caller who accepted the billing impact has not accepted that another product owns the
    snapshot, so one tick must not open both gates.

    Ablation: point the ownership gate at `acknowledge_billing_impact`.
    """
    node = FakeInstanceNode(snapshots=[{"name": UPSTREAM}])
    response = _restore(
        node, UPSTREAM, acknowledge_billing_impact=True, reason="I meant the other gate"
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["error"] == "upstream_snapshot_acknowledgement_required"
    assert not _touched(node, "create_snapshot")


# ── the guard that must not widen with ownership ───────────────────────────────────────────


@pytest.mark.parametrize(
    "prefix", ["ops-", "pre-app-", "snap-"], ids=["operator", "manual", "upstream"]
)
@pytest.mark.parametrize(
    "shape",
    [
        "x/../../instances/victim",
        "x%2f..%2fvictim",
        "x" * 90,
        "x?project=other",
    ],
)
def test_a_hostile_name_never_reaches_a_request_path_in_any_class(prefix, shape):
    """Ablation: delete the _NAME_RE check from assert_snapshot_name.

    The confirmation and the acknowledgement are both supplied, so the refusal cannot come from
    the ownership gate. Without them the manual and upstream cases would pass on the ownership
    refusal and leave the injection guard untested in two of the three classes.
    """
    name = prefix + shape
    node = FakeInstanceNode(snapshots=[{"name": name}])
    response = _delete(
        node,
        name,
        confirm_snapshot=name,
        acknowledge_upstream_snapshot=True,
        reason="probing the guard",
    )
    # 422 too: `confirm_snapshot` is a bounded query parameter, so FastAPI refuses an over-long
    # name supplied as its own confirmation before the handler runs. Each status means the name
    # never became a request path.
    assert response.status_code in (403, 404, 422), response.text
    assert not _touched(node, "delete_snapshot")


def test_the_ownership_gate_refuses_a_name_it_was_handed_unvalidated():
    """The classifier is total: a name it does not recognise, a blank one included, is `manual`.

    Ablation: drop the assert_snapshot_name call from enforce_snapshot_ownership. A blank
    confirmation then equals the blank name, so the typed-confirmation gate opens on a name
    nothing validated.
    """
    from fastapi import HTTPException

    from app.core.snapshot_ownership_gate import enforce_snapshot_ownership

    with pytest.raises(HTTPException) as raised:
        enforce_snapshot_ownership("", INSTANCE, "delete", confirm_snapshot="")
    assert raised.value.status_code == 403


# ── what the audit trail records ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name,code",
    [
        (MANUAL, "snapshot_confirmation_required"),
        (UPSTREAM, "upstream_snapshot_acknowledgement_required"),
    ],
)
def test_an_ownership_refusal_is_recorded(audited, name, code):
    """Ablation: raise from the gate without the auditing wrapper.

    An ownership refusal is the most alert-worthy refusal on this surface, so it is audited like
    the billing denial.
    """
    node = FakeInstanceNode(snapshots=[{"name": name}])
    assert _delete(node, name).status_code == 403

    denials = [row for row in audited if row.get("result") == "denied"]
    assert len(denials) == 1, audited
    assert denials[0]["action"] == "instance.snapshot.delete"
    assert denials[0]["error_message"] == code
    assert denials[0]["details"]["snapshot_origin"] == (
        "manual" if name == MANUAL else "upstream"
    )


def test_a_confirmed_hand_taken_delete_writes_no_override_record(audited):
    """A confirmed delete of a hand-taken snapshot is not an override.

    Ablation: emit an ownership_override row for the manual class. Alerting on operators
    confirming their own snapshots would bury the rows where someone acted on a snapshot another
    product owns.
    """
    node = FakeInstanceNode(snapshots=[{"name": MANUAL}])
    assert _delete(node, MANUAL, confirm_snapshot=MANUAL).status_code == 200

    overrides = [row for row in audited if "override" in row.get("action", "")]
    assert overrides == [], overrides
