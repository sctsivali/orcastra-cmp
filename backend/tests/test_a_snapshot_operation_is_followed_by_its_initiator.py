"""Only the caller who started a snapshot operation can read how it ended.

`GET /operations/{id}` needs the project's single owning organization, and a shared cluster's
`default` project has none, so not even an Admin could follow a slow snapshot delete there.
Each snapshot write records its initiator, and `/instances/{name}/snapshot-operations/{id}`
answers that initiator only, on that instance.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.snapshots import operation_initiator
from tests._snapshot_fakes import HOST, INSTANCE, PROJECT, FakeClient, FakeInstanceNode, service

OP = "3e52da73-04ab-42ab-bd8c-005b5107e8b5"
TARGET = "ops-target-20260908T000000Z"
# require_roles answers a request without a token as the dev user when auth is off, as in these
# tests; it carries no `sub`, so its actor is its user id under the "user:" prefix.
ME = "user:dev-user"


class _Cache:
    def __init__(self):
        self.rows = {}

    def get(self, namespace, key):
        return self.rows.get((namespace, key))

    def set(self, namespace, key, value, ttl=None):
        self.rows[(namespace, key)] = value


@pytest.fixture
def cache(monkeypatch):
    store = _Cache()
    monkeypatch.setattr(operation_initiator, "get_cache_manager", lambda: store)
    return store


@pytest.fixture(autouse=True)
def _no_webhook(monkeypatch):
    from app.routers import instance_snapshots

    monkeypatch.setattr(instance_snapshots, "schedule_orcahub_event", lambda *a, **k: None)


def _app(node) -> TestClient:
    from app.core.org_scope import org_scope_dependency
    from app.core.rbac import get_current_user
    from app.routers import instance_snapshot_follow, instance_snapshots
    from app.services.lxd_service import get_lxd_service

    app = FastAPI()
    app.include_router(instance_snapshots.router)
    app.include_router(instance_snapshot_follow.router)
    svc = service(FakeClient(node))
    app.dependency_overrides[get_lxd_service] = lambda: svc
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def _read(node, op=OP, instance=INSTANCE, project=PROJECT):
    return _app(node).get(
        f"/instances/{instance}/snapshot-operations/{op}", params={"host": HOST, "project": project}
    )


def _record(actor=ME, instance=INSTANCE, project=PROJECT, op=OP):
    operation_initiator.remember(HOST, op, actor=actor, project=project, instance=instance)


def test_the_initiator_reads_how_the_operation_ended(cache):
    node = FakeInstanceNode()
    node.operation_status = {OP: "Failure"}
    node.operation_err = {OP: "snapshot is in use"}
    _record()
    response = _read(node)
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "Failure", "status_code": None, "err": "snapshot is in use"}


@pytest.mark.parametrize(
    "record",
    [
        {"actor": "sub:someone-else"},
        {"instance": "ct-qa-other-0002"},
        {"project": "default"},
    ],
    ids=["another caller", "another instance", "another project"],
)
def test_a_record_that_names_anything_else_is_a_404(cache, record):
    _record(**record)
    response = _read(FakeInstanceNode())
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Operation not found"


def test_no_record_is_the_same_404(cache):
    response = _read(FakeInstanceNode())
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Operation not found"


def test_an_unreadable_cache_is_the_same_404(monkeypatch):
    def _down():
        raise ConnectionError("redis is down")

    monkeypatch.setattr(operation_initiator, "get_cache_manager", _down)
    assert _read(FakeInstanceNode()).status_code == 404


def test_an_operation_id_that_is_not_one_is_refused_before_any_lookup(cache):
    _record(op="../../1.0/certificates")
    response = _app(FakeInstanceNode()).get(
        f"/instances/{INSTANCE}/snapshot-operations/not-an-operation",
        params={"host": HOST, "project": PROJECT},
    )
    assert response.status_code == 422, response.text


def test_every_snapshot_write_records_its_initiator(cache):
    node = FakeInstanceNode(snapshots=[{"name": TARGET}])
    client = _app(node)
    assert client.post(
        f"/instances/{INSTANCE}/snapshots",
        json={"host": HOST, "project": PROJECT, "label": "before upgrade"},
    ).status_code in (201, 202)
    assert client.post(
        f"/instances/{INSTANCE}/snapshots/{TARGET}/restore",
        json={"host": HOST, "project": PROJECT, "confirm_name": INSTANCE, "safety_snapshot": False},
    ).status_code == 200
    assert client.delete(
        f"/instances/{INSTANCE}/snapshots/{TARGET}", params={"host": HOST, "project": PROJECT}
    ).status_code == 200

    recorded = {key: value for (_ns, key), value in cache.rows.items()}
    for op in ("op-create-1", "op-restore", "op-delete"):
        row = recorded.get(f"{operation_initiator.INITIATOR_KEY_PREFIX}{HOST}:{op}")
        assert row == {"actor": ME, "host": HOST, "project": PROJECT, "instance": INSTANCE}, (op, recorded)


def test_a_record_that_cannot_be_written_does_not_fail_the_write(monkeypatch):
    class _Broken:
        def set(self, *a, **k):
            raise ConnectionError("redis is down")

    monkeypatch.setattr(operation_initiator, "get_cache_manager", lambda: _Broken())
    node = FakeInstanceNode(snapshots=[{"name": TARGET}])
    response = _app(node).delete(
        f"/instances/{INSTANCE}/snapshots/{TARGET}", params={"host": HOST, "project": PROJECT}
    )
    assert response.status_code == 200, response.text


def test_the_daemons_text_is_redacted_in_the_200(cache):
    """A 200 never reaches the 5xx sanitizer, so the route redacts the operation's own text."""
    node = FakeInstanceNode()
    node.operation_status = {OP: "Failure"}
    node.operation_err = {OP: "Failed to copy https://10.1.1.12:8443/1.0/storage-pools/default from /var/snap/lxd/common/lxd/storage-pools/default/containers/x"}
    _record()
    err = _read(node).json()["err"]
    assert "https://" not in err and "/var/snap/lxd/common" not in err, err
    assert err.startswith("Failed to copy"), err


def test_the_actor_is_the_tokens_subject_not_its_email():
    from app.core.auth import AuthenticatedUser

    alice = AuthenticatedUser(user_id="alice@example.com", email="alice@example.com", raw_claims={"sub": "abc123"})
    assert operation_initiator.actor_of(alice) == "sub:abc123"
    # Another account whose email equals alice's subject still cannot be her.
    other = AuthenticatedUser(user_id="abc123", email="abc123", raw_claims={"sub": "zzz999"})
    assert operation_initiator.actor_of(other) != operation_initiator.actor_of(alice)
    # Without a subject the user id is used, under a prefix no subject can produce.
    assert operation_initiator.actor_of(AuthenticatedUser(user_id="sub:abc123")) == "user:sub:abc123"


def test_a_caller_without_snapshot_access_is_refused_before_any_lookup(cache, monkeypatch):
    from fastapi import HTTPException

    from app.routers import instance_snapshots

    def _refuse(host, project, instance, permission):
        raise HTTPException(status_code=403, detail="no snapshot permission")

    looked_up = []
    monkeypatch.setattr(instance_snapshots, "verify_tenant_permission", _refuse)
    monkeypatch.setattr(operation_initiator, "recall", lambda *a: looked_up.append(a))
    node = FakeInstanceNode()
    response = _read(node)
    assert response.status_code == 403, response.text
    assert looked_up == [], "the record was consulted before the gate"
    assert node.recorded == [], "the cluster was asked before the gate"
