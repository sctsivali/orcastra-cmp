"""A worker refused with a certificate that was rotated elsewhere switches instead of failing.

Four workers each hold the certificate they connected with, and nothing tells three of them when
the fourth renews it. Once the previous trust entry is removed, a stale worker is refused. Instead
of evicting, tripping the breaker and answering 503 for a minute, it reads the active certificate
once and, if that is a different one, connects with it. The same certificate, or no answer from
Vault, leaves the refusal on the unauthorized path.
"""

import threading
import time

import pytest

from app.services.lxd import health
from app.services.lxd.cert_rotation import CertificateRotationMixin
from app.services.lxd.connection import LXDConnectionManager
from app.services.lxd.exceptions import LXDAuthorizationError, LXDTransportPinError
from tests._cert_rotation_fakes import make_cert

CLUSTER = "rotated-cluster"
STALE, STALE_KEY = make_cert("stale")
FRESH, FRESH_KEY = make_cert("fresh")


@pytest.fixture(autouse=True)
def _clean_breaker():
    health.clear(CLUSTER)
    yield
    health.clear(CLUSTER)


class _Manager(CertificateRotationMixin):
    def __init__(self, active=(FRESH, FRESH_KEY), connect=None):
        self._connection_info = {
            CLUSTER: {"endpoint": "https://c:8443", "cert_content": STALE, "key_content": STALE_KEY, "verify": False}
        }
        self._clients = {}
        self.reads = 0
        self.connects = []
        self.registered = []
        self._active = active
        self._connect = connect

    def _read_active_certificate(self, cluster_id):
        self.reads += 1
        time.sleep(0.05)
        return self._active

    def connect_with_vault_cert(self, cluster_id, endpoint, cert, key, verify=False, force=False, expected_server_fingerprint=None):
        self.connects.append((cert, force))
        if self._connect:
            return self._connect()
        self._connection_info[cluster_id] = {"endpoint": endpoint, "cert_content": cert, "key_content": key}
        self._clients[cluster_id] = object()
        return self._clients[cluster_id]

    def register_cluster(self, cluster_id):
        self.registered.append(cluster_id)


def _refused(manager):
    return dict(manager._connection_info[CLUSTER])


def test_a_different_active_certificate_reconnects_with_force():
    manager = _Manager()
    client = manager._try_rotated_certificate(CLUSTER, _refused(manager))
    assert client is not None and manager.connects == [(FRESH, True)]


def test_the_same_certificate_keeps_todays_behaviour():
    manager = _Manager(active=(STALE, STALE_KEY))
    assert manager._try_rotated_certificate(CLUSTER, _refused(manager)) is None
    assert manager.connects == []


def test_vault_not_answering_keeps_todays_behaviour():
    manager = _Manager(active=None)
    assert manager._try_rotated_certificate(CLUSTER, _refused(manager)) is None
    assert manager.connects == []


def test_a_refused_fresh_certificate_is_left_to_the_unauthorized_path():
    def refuse():
        raise LXDAuthorizationError("not trusted")

    manager = _Manager(connect=refuse)
    assert manager._try_rotated_certificate(CLUSTER, _refused(manager)) is None


def test_a_transport_failure_after_a_refusal_stays_unauthorized_not_offline():
    def broken():
        raise ConnectionError("reset")

    manager = _Manager(connect=broken)
    assert manager._try_rotated_certificate(CLUSTER, _refused(manager)) is None
    assert health.get_kind(CLUSTER) == health.KIND_UNAUTHORIZED


def test_a_pin_violation_is_never_swallowed():
    def pinned():
        raise LXDTransportPinError("changed")

    manager = _Manager(connect=pinned)
    with pytest.raises(LXDTransportPinError):
        manager._try_rotated_certificate(CLUSTER, _refused(manager))


def test_concurrent_refusals_read_vault_once():
    manager = _Manager()
    refused = _refused(manager)
    results = []
    threads = [threading.Thread(target=lambda: results.append(manager._try_rotated_certificate(CLUSTER, refused))) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert manager.reads == 1 and len(manager.connects) == 1
    assert all(result is not None for result in results)


def test_adopting_keeps_the_cluster_listed_in_this_worker():
    manager = _Manager()
    assert manager.adopt_vault_certificate(CLUSTER, "https://c:8443", FRESH, FRESH_KEY) is not None
    assert manager.registered == [CLUSTER]
    assert manager.active_certificate_fingerprint(CLUSTER) is not None


def test_a_failed_adoption_returns_none_and_lists_nothing():
    def refuse():
        raise LXDAuthorizationError("no")

    manager = _Manager(connect=refuse)
    assert manager.adopt_vault_certificate(CLUSTER, "https://c:8443", FRESH, FRESH_KEY) is None
    assert manager.registered == []


class _Response:
    status_code = 200

    def __init__(self, auth):
        self._auth = auth

    def json(self):
        return {"metadata": {"auth": self._auth}}


class _Client:
    def __init__(self, auth):
        self.host_info = {"auth": auth}
        self.api = type("Api", (), {"get": lambda _self, **_: _Response(auth)})()


def test_the_real_refusal_branch_switches_and_does_not_trip(monkeypatch):
    """Through _auto_reconnect itself: the stale cert is refused, the Vault one is trusted."""
    manager = LXDConnectionManager()
    monkeypatch.setattr("app.core.org_scope.get_current_allowed_clusters", lambda: None, raising=False)
    monkeypatch.setattr(manager, "_create_temp_cert_files", lambda cid, cert, key: ("/tmp/c.crt", "/tmp/c.key"))
    monkeypatch.setattr(manager, "_enforce_certificate_pin", lambda cid, endpoint: None)
    monkeypatch.setattr(manager, "_configure_client_session", lambda *a, **k: None)
    monkeypatch.setattr(manager, "_read_active_certificate", lambda cid: (FRESH, FRESH_KEY))
    monkeypatch.setattr("app.services.activity_logger.get_activity_logger", lambda: type("L", (), {"log_activity": lambda *a, **k: None})())
    manager._connection_info[CLUSTER] = {
        "endpoint": "https://c:8443", "cert_content": STALE, "key_content": STALE_KEY, "verify": False,
    }

    def client_for(**kwargs):
        return _Client("trusted" if manager._connection_info.get(CLUSTER, {}).get("cert_content") == FRESH else "untrusted")

    monkeypatch.setattr("app.services.lxd.connection.pylxd.Client", client_for)

    client = manager._auto_reconnect(CLUSTER)

    assert client is not None
    assert manager._connection_info[CLUSTER]["cert_content"] == FRESH
    assert not health.is_tripped(CLUSTER), "no 503 minute for a worker that only held an old certificate"
