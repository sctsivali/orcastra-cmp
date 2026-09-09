"""A snapshot taken by hand is the operator's own, and the console stopped pretending otherwise.

The surface recognised two classes, `ops-` and everything else, so a snapshot an operator took
over SSH was refused with "managed elsewhere" alongside the ones the upstream product genuinely
owns. `udesk-semanan` carries three of them, and the feature doc names those as the PROBLEM this
feature exists to solve rather than as anyone else's property.

Three classes now, three gates: straight through for ours, a typed confirmation for one taken on
the host, an acknowledgement and a reason for one upstream owns. Every test here names the
ablation that turns it red.
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
# The real names off this fleet, from documentation/instances/operator-snapshots-and-mobility.md.
# Inventing a plausible one would have hidden that `pre-atlas-...` starts with `pre-` and is still
# not `pre-restore-`, which is the exact distinction being drawn.
MANUAL = "pre-atlas-guacamole-20260901T134000Z"
OTHER_MANUAL = "c35-cia-pre-20260906T050115Z"
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

    # The boolean stays, and stays the narrower question: did THIS CONSOLE make it.
    for name in (MANUAL, UPSTREAM):
        row = _normalize({"name": name}, INSTANCE)
        assert row["operator_managed"] is False
    assert _normalize({"name": OURS}, INSTANCE)["operator_managed"] is True


def test_the_snapshot_row_model_carries_every_field_the_service_stamps():
    """Ablation: add a field to _normalize and forget SnapshotInfo.

    A typed response model is an allowlist and drops what it does not declare, silently. That is
    exactly how `size_measured` was lost from the per-instance route, so this replaces a comment
    about it with a check.
    """
    from app.models.schemas import SnapshotInfo
    from app.services.lxd.snapshots import _normalize

    row = _normalize({"name": MANUAL, "size": 10}, INSTANCE)
    kept = SnapshotInfo(**row).model_dump()
    missing = sorted(set(row) - set(kept))
    assert missing == [], f"the response model drops what the service stamps: {missing}"


def test_a_hand_taken_snapshot_does_not_consume_the_operator_cap():
    """Ablation: widen count_operator_snapshots to the new classification.

    The cap exists so a snapshot taken outside this console cannot make the count wrong in
    either direction. Reclassifying must not quietly spend an operator's budget of five on
    someone's manual restore points.
    """
    from app.services.lxd.snapshots import count_operator_snapshots

    node = FakeInstanceNode(
        snapshots=[{"name": OURS}, {"name": MANUAL}, {"name": OTHER_MANUAL}, {"name": UPSTREAM}]
    )
    assert count_operator_snapshots(FakeClient(node), INSTANCE, PROJECT) == 1


# ── the hand-taken class ───────────────────────────────────────────────────────────────────


def test_a_hand_taken_snapshot_is_deletable_once_its_name_is_typed():
    """Ablation: restore the catch-all `must start with ops-` refusal."""
    node = FakeInstanceNode(snapshots=[{"name": MANUAL}])
    response = _delete(node, MANUAL, confirm_snapshot=MANUAL)
    assert response.status_code == 200, response.text
    assert _touched(node, "delete_snapshot"), "the daemon must have been asked"


def test_a_hand_taken_snapshot_is_not_deleted_without_the_typed_name():
    """Ablation: let MANUAL fall through the gate.

    The recorded-calls assertion is the load-bearing half: it is what catches a 403 that arrives
    AFTER the daemon was already asked.
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
    """Restore does not ask for a second typed string, and that is deliberate.

    Ablation: make the gate demand `confirm_snapshot` on restore too. Two typed fields in one
    dialog is what produces copy-paste without reading, which the confirm dialog's own docblock
    argues at length.
    """
    node = FakeInstanceNode(snapshots=[{"name": MANUAL}])
    response = _restore(node, MANUAL)
    assert response.status_code == 200, response.text

    # And the instance name is still genuinely required.
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
    """The two gates read different fields, and this is why.

    A live QA spec posts an upstream restore carrying `acknowledge_billing_impact` and a reason,
    and asserts 403. Had the ownership gate read that field, that spec would have stopped proving
    a refusal and started performing a real restore on a real instance while staying green.

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
    "prefix", ["ops-", "pre-atlas-", "snap-"], ids=["operator", "manual", "upstream"]
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

    important: the confirmation and the acknowledgement are BOTH supplied, so the refusal cannot
    come from the ownership gate. Without that, the manual and upstream rows would pass off the
    ownership decision and the injection guard would be untested in two classes out of three,
    which is precisely what fusing the two functions used to hide.
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
    # 422 belongs here: `confirm_snapshot` is a bounded query parameter, so an over-long name
    # supplied as its own confirmation is refused by FastAPI before the handler runs. All three
    # are the same claim, which is the only one that matters: it never became a request path.
    assert response.status_code in (403, 404, 422), response.text
    assert not _touched(node, "delete_snapshot")


def test_the_ownership_gate_refuses_a_name_it_was_handed_unvalidated():
    """The classifier is total and falls back to the MOST permissive class.

    Ablation: drop the assert_snapshot_name call from enforce_snapshot_ownership. A blank name
    then classifies as `manual`, and a blank confirmation equals it, so the weakest gate opens on
    a name nothing validated.
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

    The billing denial has been audited since it shipped and the ownership one was invisible, so
    the most alert-worthy refusal on this surface left no trace at all.
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
    """A speed bump is not an override.

    Ablation: emit an ownership_override row for the manual class. Alerting on an operator
    confirming their own snapshot would bury the rows that mean someone acted on another
    product's property.
    """
    node = FakeInstanceNode(snapshots=[{"name": MANUAL}])
    assert _delete(node, MANUAL, confirm_snapshot=MANUAL).status_code == 200

    overrides = [row for row in audited if "override" in row.get("action", "")]
    assert overrides == [], overrides
