"""The circuit breaker sits at `get_client`, the one chokepoint every caller shares.

`connection_manager.get_client` revalidates the pooled connection with a live request and, when
that fails, rebuilds the pylxd client with a TLS handshake and a verifying GET. Against an
unreachable cluster that takes seconds, and many `async def` handlers await it inline, so a few
concurrent requests park the event loop past the container healthcheck timeout and the restart
drops every live console and terminal session. Checking the breaker here makes the repeat cost of
a dead cluster zero without touching a caller, because returning None is already part of
`get_client`'s contract.
"""

import time

import pytest

from app.services.lxd import health
from app.services.lxd.connection import LXDConnectionManager

CLUSTER = "cluster-under-test"


@pytest.fixture(autouse=True)
def _clean_breaker():
    health.clear(CLUSTER)
    yield
    health.clear(CLUSTER)


@pytest.fixture
def manager(monkeypatch):
    mgr = LXDConnectionManager()
    # Outside a request the org-scope filter returns None (unrestricted). Pin it so the test
    # cannot depend on ambient state.
    monkeypatch.setattr(
        "app.core.org_scope.get_current_allowed_clusters", lambda: None, raising=False
    )
    return mgr


def test_a_tripped_cluster_returns_none_without_attempting_a_connection(manager) -> None:
    attempts = []
    manager._auto_reconnect = lambda cid: attempts.append(cid)  # type: ignore[assignment]

    health.record_failure(CLUSTER, "unreachable")
    assert manager.get_client(CLUSTER) is None
    assert attempts == [], "a tripped cluster must not be dialled again"


def test_an_untripped_cluster_is_still_attempted(manager) -> None:
    attempts = []

    def _reconnect(cid):
        attempts.append(cid)
        return "client"

    manager._auto_reconnect = _reconnect  # type: ignore[assignment]

    assert manager.get_client(CLUSTER) == "client"
    assert attempts == [CLUSTER]


def test_a_failed_reconnect_trips_the_breaker_so_the_next_caller_is_free(manager) -> None:
    """The property that turns an O(requests) cost into O(1) per TTL."""
    calls = {"n": 0}

    # A transport failure, the only thing that may trip the breaker. Missing connection info must
    # not trip it (see the next test).
    manager._connection_info[CLUSTER] = {
        "endpoint": "https://192.0.2.1:8443", "cert_content": "x", "key_content": "y",
    }

    def _explode(*_args, **_kwargs):
        calls["n"] += 1
        raise OSError("connection refused")

    manager._create_temp_cert_files = _explode  # type: ignore[assignment]
    assert manager.get_client(CLUSTER) is None
    assert health.is_tripped(CLUSTER), "a transport failure must record it"
    assert calls["n"] == 1

    assert manager.get_client(CLUSTER) is None
    assert calls["n"] == 1, "the second caller paid a connection cost anyway"


def test_missing_local_connection_info_must_not_trip_the_breaker(manager) -> None:
    """Tripping here would seal every path that can reopen the cluster.

    ``_connection_info`` is per process, so a cluster registered after this worker booted, or one
    whose info a connect error cleared, has no entry. The repair is the Vault path in
    ``ensure_connection`` and the background healer, both of which consult this breaker. A trip
    here would re-trip on every retry before any Vault read and keep a healthy cluster unreachable
    in that worker until the process restarts.
    """
    manager._connection_info.pop(CLUSTER, None)

    assert manager.get_client(CLUSTER) is None, "no local info still means no client"
    assert not health.is_tripped(CLUSTER), (
        "a missing local cache entry is not a cluster failure; tripping it locks out the Vault "
        "reconnect that is the designed repair"
    )


def test_recovery_is_immediate_on_success_not_after_the_ttl(manager) -> None:
    health.record_failure(CLUSTER, "unreachable")
    assert manager.get_client(CLUSTER) is None

    # Whatever brought the cluster back (a successful connect elsewhere) clears it, and the
    # very next call must go through rather than waiting out the TTL.
    health.clear(CLUSTER)
    manager._auto_reconnect = lambda cid: "client"  # type: ignore[assignment]
    assert manager.get_client(CLUSTER) == "client"


def test_the_breaker_expires_so_a_cluster_is_reprobed(manager) -> None:
    """A breaker that never expires is a blacklist. Recovery must not need a restart."""
    health.record_failure(CLUSTER, "unreachable")
    assert health.is_tripped(CLUSTER, ttl=60.0)

    # Same clock, evaluated with an already-elapsed window.
    time.sleep(0.05)
    assert not health.is_tripped(CLUSTER, ttl=0.01)

    manager._auto_reconnect = lambda cid: "client"  # type: ignore[assignment]
    assert manager.get_client(CLUSTER) == "client"


def test_a_lapsed_trip_is_still_a_known_outage_until_a_connect_succeeds(manager) -> None:
    """Expiry re-opens the door to a retry. It is not evidence that the cluster came back.

    Fleet reads ask this to tell a dead peer apart from a worker that has never connected. Read
    as is_tripped, every dead cluster would look like a cold worker between two retries.
    """
    assert not health.known_unreachable(CLUSTER)

    health.record_failure(CLUSTER, "certificate not trusted", kind=health.KIND_UNAUTHORIZED)
    time.sleep(0.05)
    assert not health.is_tripped(CLUSTER, ttl=0.01)
    assert health.known_unreachable(CLUSTER)
    # The verdict outlives the trip with it, so a reader can still tell refused from offline.
    assert health.last_failure(CLUSTER) == ("certificate not trusted", health.KIND_UNAUTHORIZED)

    health.clear(CLUSTER)
    assert not health.known_unreachable(CLUSTER)
    assert health.last_failure(CLUSTER) is None


def test_the_breaker_remembers_whether_the_host_answered(manager) -> None:
    """A tripped cluster still has to be classifiable.

    Without the kind, every cluster the breaker holds reads as "offline" for 60 seconds,
    including one that answered and refused the dashboard's certificate, which needs a
    different fix.
    """
    health.record_failure(CLUSTER, "certificate not trusted", kind=health.KIND_UNAUTHORIZED)
    assert health.get_kind(CLUSTER) == health.KIND_UNAUTHORIZED
    assert health.get_reason(CLUSTER) == "certificate not trusted"


def test_the_default_kind_is_offline(manager) -> None:
    """A caller that names no kind is assumed to have failed to reach the cluster."""
    health.record_failure(CLUSTER, "connection refused")
    assert health.get_kind(CLUSTER) == health.KIND_OFFLINE


def test_clearing_drops_the_kind_with_the_entry(manager) -> None:
    health.record_failure(CLUSTER, "certificate not trusted", kind=health.KIND_UNAUTHORIZED)
    health.clear(CLUSTER)
    assert health.get_kind(CLUSTER) is None
    assert health.get_reason(CLUSTER) is None


def test_a_generic_failure_never_downgrades_an_authorization_one() -> None:
    """A host that answered and refused the certificate must not read as offline.

    connect_with_vault_cert records KIND_UNAUTHORIZED, and ensure_connection's broad
    `except Exception` then records the same cluster again with the default kind. The breaker
    must keep the specific kind, or it serves "Offline" to every reader for the rest of its TTL.
    """
    from app.services.lxd.exceptions import LXDAuthorizationError
    from app.services.lxd.lxd_service import LXDService

    cluster = "blitz001"
    health.clear(cluster)

    class _Vault:
        # The connect path reads only <cluster>/metadata, never <cluster>/secrets.
        def vault_read(self, path):
            assert path.endswith("/metadata"), f"unexpected Vault read on a connect: {path}"
            return {"endpoint": "https://10.1.1.49:9443"}

    class _Certs:
        vault = _Vault()

        def get_cluster_certificate(self, cid):
            return {"client.crt": "crt", "client.key": "key"}

    class _Conn:
        def get_client(self, cid, *a, **kw):
            return None

        def connect_with_vault_cert(self, cluster_id, **kw):
            # As the real one does: record the specific kind, then re-raise.
            health.record_failure(
                cluster_id, "certificate not trusted", kind=health.KIND_UNAUTHORIZED
            )
            raise LXDAuthorizationError(f"Certificate not trusted by {cluster_id}")

    svc = LXDService.__new__(LXDService)
    svc.connection_manager = _Conn()
    svc.cert_manager = _Certs()

    assert svc.ensure_connection(cluster) is False
    assert health.get_kind(cluster) == health.KIND_UNAUTHORIZED, (
        "a host that answered and refused us must not be recorded as unreachable"
    )
    health.clear(cluster)


def test_a_tripped_cluster_still_reports_its_endpoint() -> None:
    """The endpoint is registry metadata, so a tripped cluster still reports it.

    Read after the breaker check, every tripped cluster would report endpoint="unknown", which
    the node picker prints under the cluster's name. The breaker skips an 8 s connect, not a
    local Vault read.
    """
    from app.services.lxd.lxd_service import LXDService

    cluster = "blitzkrieg"
    endpoint = "https://142.132.147.94:8443"

    class _Vault:
        def vault_read(self, path):
            return {"endpoint": endpoint}

    class _Certs:
        def __init__(self):
            self.vault = _Vault()

    class _Settings:
        vault_clusters_path = "secret/data/clusters"

    svc = LXDService.__new__(LXDService)
    svc.cert_manager = _Certs()

    health.record_failure(cluster, "connection refused")
    try:
        status = svc._check_single_node_status(cluster, _Settings())
        assert status.endpoint == endpoint, status.endpoint
        assert status.connected is False
    finally:
        health.clear(cluster)
