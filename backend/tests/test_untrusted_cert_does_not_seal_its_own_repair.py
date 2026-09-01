"""An untrusted certificate must not seal the door that would replace it.

`_auto_reconnect` rebuilds the pylxd client from a copy of the certificate held in
`_connection_info`. When the peer answers and refuses that certificate, the old code recorded
the failure and returned, keeping the refused bytes cached. Every later attempt then presented
the same certificate and was refused again: 1351 `cert not trusted` events against 265
successful reconnects, measured on the dev fleet.

Keeping the entry is not merely wasteful, it is self-sealing, and through two independent
gates:

  * `ensure_connection` consults the breaker this path trips, so the Vault re-read is skipped
    for the TTL, and
  * the background healer checks `has_connection_info` BEFORE it consults the breaker, so a
    retained entry stops it reaching Vault at all, for as long as the entry survives.

The second is the one that makes the state outlive the 60s TTL: the entry never expires, so the
healer is permanently short-circuited and only a `force=True` caller can repair the cluster.
Dropping the entry leaves exactly one gate, the breaker's own TTL, which does expire.

What must NOT change, and is asserted here as well as in test_connection_breaker:
the failure is still RECORDED, and still as KIND_UNAUTHORIZED. Five callers derive
`reachable = (kind == KIND_UNAUTHORIZED)`, so clearing it, or recording it as the default
offline kind, reports a cluster that answered and refused us as plain Offline.
"""

import pytest

from app.services.lxd import health
from app.services.lxd.connection import LXDConnectionManager

CLUSTER = "cluster-under-test"


@pytest.fixture(autouse=True)
def _clean_breaker():
    health.clear(CLUSTER)
    yield
    health.clear(CLUSTER)


class _Response:
    def __init__(self, auth: str, status_code: int = 200):
        self.status_code = status_code
        self._auth = auth

    def json(self):
        return {"metadata": {"auth": self._auth}}


class _Api:
    def __init__(self, auth: str):
        self._auth = auth

    def get(self, **_kwargs):
        return _Response(self._auth)


class _Client:
    def __init__(self, auth: str):
        self.api = _Api(auth)


@pytest.fixture
def manager(monkeypatch):
    mgr = LXDConnectionManager()
    monkeypatch.setattr(
        "app.core.org_scope.get_current_allowed_clusters", lambda: None, raising=False
    )
    # Materialising cert files touches the tmpfs; the handshake is what is under test, not the
    # filesystem, so hand back paths without writing anything.
    monkeypatch.setattr(
        mgr, "_create_temp_cert_files", lambda cid, cert, key: ("/tmp/c.crt", "/tmp/c.key")
    )
    mgr._connection_info[CLUSTER] = {
        "endpoint": "https://cluster:8443",
        "cert_content": "STALE-CERT",
        "key_content": "STALE-KEY",
        "verify": False,
    }
    return mgr


def _reconnect_answering(monkeypatch, manager, auth: str):
    monkeypatch.setattr(
        "app.services.lxd.connection.pylxd.Client", lambda **kwargs: _Client(auth)
    )
    return manager._auto_reconnect(CLUSTER)


def test_a_refused_certificate_is_dropped_from_the_cache(manager, monkeypatch) -> None:
    """The entry the next attempt would reuse must not survive the refusal."""
    assert CLUSTER in manager._connection_info

    assert _reconnect_answering(monkeypatch, manager, "untrusted") is None

    assert CLUSTER not in manager._connection_info, (
        "the refused certificate stayed cached, so the next attempt presents it again and the "
        "healer's has_connection_info gate keeps it from ever reaching Vault"
    )


def test_dropping_it_reopens_the_healer_gate(manager, monkeypatch) -> None:
    """has_connection_info is what the healer checks before the breaker, so it is the gate."""
    assert manager.has_connection_info(CLUSTER) is True

    _reconnect_answering(monkeypatch, manager, "untrusted")

    assert manager.has_connection_info(CLUSTER) is False, (
        "the healer skips any cluster that still has connection info, so this staying True is "
        "what made the state outlive the breaker TTL"
    )


def test_the_failure_is_still_recorded_as_unauthorized(manager, monkeypatch) -> None:
    """Evicting must not turn a cluster that answered into one that looks offline."""
    _reconnect_answering(monkeypatch, manager, "untrusted")

    assert health.is_tripped(CLUSTER), (
        "the peer answered and refused us and will keep refusing, so not recording it makes "
        "every later request pay a full handshake"
    )
    assert health.get_kind(CLUSTER) == health.KIND_UNAUTHORIZED, (
        "five callers derive reachable from this kind; the default offline kind would report a "
        "cluster that answered as unreachable"
    )
    assert "not trusted" in (health.get_reason(CLUSTER) or "")


def test_a_trusted_reconnect_still_succeeds_and_clears(manager, monkeypatch) -> None:
    """The happy path must be untouched: cache kept, client pooled, breaker cleared."""
    health.record_failure(CLUSTER, "earlier failure")

    client = _reconnect_answering(monkeypatch, manager, "trusted")

    assert client is not None
    assert CLUSTER in manager._connection_info, "a working certificate must stay cached"
    assert manager._clients.get(CLUSTER) is client
    assert not health.is_tripped(CLUSTER), "a success clears the breaker immediately"


def test_eviction_is_idempotent(manager, monkeypatch) -> None:
    """Two threads can reach this at once; these dicts carry no lock, so the second must not
    raise. This is why the code uses pop rather than test-then-delete."""
    _reconnect_answering(monkeypatch, manager, "untrusted")
    assert _reconnect_answering(monkeypatch, manager, "untrusted") is None
    assert CLUSTER not in manager._connection_info


def test_missing_info_still_does_not_trip(manager) -> None:
    """The neighbouring regression must survive this change: an absent entry is the normal state
    for a cluster registered after this worker booted, and tripping on it seals the repair."""
    manager._connection_info.pop(CLUSTER, None)

    assert manager._auto_reconnect(CLUSTER) is None
    assert not health.is_tripped(CLUSTER)
