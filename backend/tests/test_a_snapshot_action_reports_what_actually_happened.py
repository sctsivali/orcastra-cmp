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


# ── the wait has to outlast itself ─────────────────────────────────────────────────────────


def test_the_settle_socket_window_outlasts_the_wait_it_asks_lxd_for():
    """A wait whose socket dies first reports "still running" for an operation that already failed.

    The pooled client reads for 8 seconds. `wait_for_operation` asks the daemon to hold the
    response for 20, so without an explicit socket timeout the read expired first on every call
    that did not settle almost immediately, `_settle` swallowed the ReadTimeout as
    `settled: False`, and the 502-on-failure branch became unreachable for anything slower than
    8 seconds. The rule is stated at guest_output.py:120-122 and was simply not applied here.
    """
    node = FakeInstanceNode(snapshots=[{"name": TARGET}])
    assert _delete(node).status_code == 200

    waits = [c for c in node.recorded if c[0] == "wait_operation"]
    assert waits, "the delete must wait on its operation"
    params, socket_timeout = waits[0][1], waits[0][2]

    asked_of_lxd = int(params.get("timeout", 0))
    assert asked_of_lxd > 0, f"no wait was requested of the daemon: {params}"
    assert socket_timeout is not None, (
        "no socket timeout was passed, so the pooled client's 8 second read applies and the wait "
        "cannot outlast itself"
    )
    _connect, read = socket_timeout
    assert read > asked_of_lxd, (
        f"the socket window ({read}s) must outlast the wait asked of LXD ({asked_of_lxd}s), "
        "or the read dies first and a real failure is reported as still running"
    )


@pytest.mark.parametrize("terminal_status", ["Failure", "Cancelled"])
def test_every_settled_non_success_is_reported_as_one(terminal_status):
    """Cancelled is settled and is not success. It used to be reported as done.

    The daemon cancels an operation on a restart mid-queue, and an operator can cancel one by hand.
    The handlers compared against "Failure" alone, so the caller was told the snapshot was created,
    deleted or restored when it had not been.
    """
    # A separate node per action: the delete removes the snapshot from the fake, and the restore
    # would then legitimately 404 on the existence check rather than on the operation outcome.
    for action in (_delete, _restore):
        node = FakeInstanceNode(snapshots=[{"name": TARGET}])
        node.operation_status = terminal_status
        node.operation_err = "operation did not complete"
        assert action(node).status_code == 502, f"{action.__name__} on {terminal_status}"


def test_a_running_operation_is_not_treated_as_a_failure():
    """The guard must not turn "still working" into an error."""
    node = FakeInstanceNode(snapshots=[{"name": TARGET}])
    node.operation_status = "Running"
    assert _delete(node).status_code == 200


# ── the name that was checked is the name that is used ─────────────────────────────────────


def test_a_name_carrying_a_newline_never_reaches_the_daemon_in_that_form():
    """A trailing newline is normalised away and the daemon is addressed with the clean name.

    note: the anchor in `_NAME_RE` is `\\Z` rather than `$` because Python's `$` matches before a
    trailing newline. That is defence in depth rather than the operative guard here: the validator
    strips first, so the newline is gone before the pattern is applied either way. What this
    asserts is the part that was actually broken, which is that the daemon is addressed with the
    validated form.
    """
    node = FakeInstanceNode(snapshots=[{"name": TARGET}])
    response = _delete(node, name=TARGET + "\n")
    assert response.status_code == 200, response.text
    deletes = [c for c in node.recorded if c[0] == "delete_snapshot"]
    assert deletes and deletes[0][1] == TARGET, (
        f"the daemon was addressed with {deletes[0][1] if deletes else None!r}"
    )


def test_the_daemon_is_addressed_with_the_name_that_was_validated():
    """The validator normalises before it checks, so its return value is the approved form.

    Discarding it meant a name was approved as `ops-target` and then sent as `" ops-target "`,
    which is a 404 on a snapshot that is really there.
    """
    node = FakeInstanceNode(snapshots=[{"name": TARGET}])
    response = _delete(node, name=f"  {TARGET}  ")
    assert response.status_code == 200, response.text

    deletes = [c for c in node.recorded if c[0] == "delete_snapshot"]
    assert deletes, "the delete must have reached the daemon"
    assert deletes[0][1] == TARGET, (
        f"the daemon was addressed with {deletes[0][1]!r}, not the validated {TARGET!r}"
    )


# ── accepted, but with nothing to wait on ──────────────────────────────────────────────────


def test_an_accepted_response_naming_no_operation_is_not_reported_as_done():
    """A 202 whose body names no operation means the work is running and unverifiable.

    `_operation_id` returned None for that case and for a genuinely synchronous 200 alike, and the
    caller treats None as "nothing to wait for, so it is finished". Every fake in this suite
    hardcoded an operation id, so the branch that makes that decision was unreachable from any
    test while a real cluster answering oddly would have been reported as success.
    """
    node = FakeInstanceNode(snapshots=[{"name": TARGET}])
    node.omit_operation = True

    response = _delete(node)
    assert response.status_code == 502, response.text
    assert "no operation" in response.text, response.text


def test_a_create_the_cluster_accepts_without_an_operation_is_not_a_201():
    node = FakeInstanceNode()
    node.omit_operation = True
    client = _client(service(FakeClient(node)))
    response = client.post(
        f"/instances/{INSTANCE}/snapshots",
        json={"host": HOST, "project": PROJECT, "label": "unverifiable"},
    )
    assert response.status_code >= 400, response.text
