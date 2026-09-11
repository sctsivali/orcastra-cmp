"""Every worker learns a cluster registered after boot, and a slow cluster is not "unreachable".

Seen in production with WEB_CONCURRENCY=2 and reproduced on the development VM with 4: sixteen
identical reads of a list endpoint answered "C2 answered" ten times and "Could not reach C2"
six times within seconds, while the cluster answered every direct probe in 0.2 s. The whitelist
behind `get_node_names()` was synced at boot and in the one worker that served the
registration; the others omitted the cluster forever, even after the background healer had
connected it. Separately, one validation probe timing out on the read evicted the pooled
client and the reconnect's own probe timing out tripped the breaker for 60 s.
"""

import types

import pytest
import requests
import urllib3

from app.services.lxd import health
from app.services.lxd.connection import LXDConnectionManager, TimeoutHTTPAdapter
from app.services.lxd.exceptions import LXDConnectionError
from app.services import metrics_collector as mc

CLUSTER = "C2"


@pytest.fixture(autouse=True)
def _clean_breaker():
    health.clear(CLUSTER)
    yield
    health.clear(CLUSTER)


@pytest.fixture
def manager(monkeypatch):
    mgr = LXDConnectionManager()
    monkeypatch.setattr("app.core.org_scope.get_current_allowed_clusters", lambda: None, raising=False)
    return mgr


# --- the whitelist ---------------------------------------------------------------------------

def test_the_healer_tick_resyncs_the_whitelist_from_vault(manager, monkeypatch):
    """A worker that booted before the registration must list the cluster once it holds a client."""
    manager._registered_clusters = {"blitz01"}
    manager._clients[CLUSTER] = object()          # the healer had already connected it
    manager._clients["blitz01"] = object()
    assert manager.get_node_names() == ["blitz01"], "precondition: the stale whitelist hides C2"

    settings = types.SimpleNamespace(vault_enabled=True, vault_token="t", vault_addr="http://v", vault_clusters_path="secret/data/clusters")
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)
    monkeypatch.setattr("app.core.vault_client.get_vault_client", lambda addr, token: object())

    class _CertManager:
        def __init__(self, vault_client, clusters_path):
            pass

        def list_clusters(self):
            return ["blitz01", CLUSTER]

    monkeypatch.setattr("app.services.lxd_cert_manager.LXDCertificateManager", _CertManager)
    manager.get_client = lambda cid: manager._clients.get(cid)  # type: ignore[assignment]

    collector = mc.MetricsCollector()
    collector._lxd_service = types.SimpleNamespace(connection_manager=manager)
    collector._ensure_cluster_connections_blocking()

    assert sorted(manager.get_node_names()) == sorted(["blitz01", CLUSTER])


def test_an_empty_vault_answer_does_not_blank_the_whitelist(manager, monkeypatch):
    manager._registered_clusters = {"blitz01", CLUSTER}
    manager._clients[CLUSTER] = object()

    settings = types.SimpleNamespace(vault_enabled=True, vault_token="t", vault_addr="http://v", vault_clusters_path="p")
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)
    monkeypatch.setattr("app.core.vault_client.get_vault_client", lambda addr, token: object())

    class _CertManager:
        def __init__(self, *a, **k):
            pass

        def list_clusters(self):
            return []

    monkeypatch.setattr("app.services.lxd_cert_manager.LXDCertificateManager", _CertManager)
    manager.get_client = lambda cid: manager._clients.get(cid)  # type: ignore[assignment]
    collector = mc.MetricsCollector()
    collector._lxd_service = types.SimpleNamespace(connection_manager=manager)
    collector._ensure_cluster_connections_blocking()
    assert manager.get_node_names() == [CLUSTER]


def test_registering_one_cluster_never_starts_filtering_from_a_one_element_set(manager):
    manager._registered_clusters = None          # boot sync failed: unfiltered
    manager._clients["nuc1"] = object()
    manager._clients[CLUSTER] = object()
    manager.register_cluster(CLUSTER)
    assert manager._registered_clusters is None
    assert sorted(manager.get_node_names()) == [CLUSTER, "nuc1"]


def test_a_connect_attempt_is_remembered_whatever_came_of_it(manager, monkeypatch):
    """The list routers cache a body naming a cluster this worker tried and found down, and
    refuse to cache one naming a cluster it has not tried. Both outcomes of a connect, and the
    breaker refusing to try, must therefore count as tried."""
    from app.services.lxd.fanout import unattempted

    assert not manager.has_attempted(CLUSTER)
    assert unattempted([CLUSTER, "nuc1"], manager) == [CLUSTER, "nuc1"]

    monkeypatch.setattr("app.services.lxd.connection.pylxd.Client", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down")))
    with pytest.raises(LXDConnectionError):
        manager.connect_with_vault_cert(CLUSTER, "https://127.0.0.1:1", "c", "k", verify=False)
    assert manager.has_attempted(CLUSTER), "a failed connect is an attempt"
    assert unattempted([CLUSTER, "nuc1"], manager) == ["nuc1"]

    # the breaker now refuses the next try without contacting the peer: still an attempt
    assert health.is_tripped(CLUSTER)
    assert manager.connect_with_vault_cert(CLUSTER, "https://127.0.0.1:1", "c", "k", verify=False) is None
    assert manager.has_attempted(CLUSTER)

    # a stub manager without the method is read conservatively: nothing tried, nothing cached
    assert unattempted(["x"], object()) == ["x"]


# --- the slow probe --------------------------------------------------------------------------

def _read_timeout_via_connection_error():
    return requests.exceptions.ConnectionError(
        urllib3.exceptions.MaxRetryError(
            None, "/1.0", reason=urllib3.exceptions.ReadTimeoutError(None, "/1.0", "Read timed out")
        )
    )


class _Client:
    def __init__(self, failures):
        self._failures = list(failures)
        self.api = self
        self.host_info = {}

    def get(self, timeout=None):
        exc = self._failures.pop(0) if self._failures else None
        if exc is not None:
            raise exc
        return types.SimpleNamespace(status_code=200, json=lambda: {"metadata": {"auth": "trusted"}})


def test_one_read_timeout_keeps_the_pooled_client_and_the_breaker_untouched(manager):
    client = _Client([requests.exceptions.ReadTimeout("slow")])
    manager._clients[CLUSTER] = client
    reconnects = []
    manager._auto_reconnect = lambda cid: reconnects.append(cid)  # type: ignore[assignment]

    assert manager.get_client(CLUSTER) is client
    assert reconnects == []
    assert not health.is_tripped(CLUSTER)


def test_a_burst_of_read_timeouts_on_one_slow_moment_counts_as_one(manager):
    """Five parallel callers hitting the same slow probe must not evict the client between them."""
    client = _Client([_read_timeout_via_connection_error() for _ in range(5)])
    manager._clients[CLUSTER] = client
    reconnects = []
    manager._auto_reconnect = lambda cid: reconnects.append(cid) or None  # type: ignore[assignment]
    for _ in range(5):
        assert manager.get_client(CLUSTER) is client
    assert reconnects == []


def test_a_second_read_timeout_after_the_spacing_takes_the_reconnect_path(manager):
    client = _Client([_read_timeout_via_connection_error(), _read_timeout_via_connection_error()])
    manager._clients[CLUSTER] = client
    reconnects = []
    manager._auto_reconnect = lambda cid: reconnects.append(cid) or None  # type: ignore[assignment]

    assert manager.get_client(CLUSTER) is client
    count, first_at = manager._slow_validations[CLUSTER]
    manager._slow_validations[CLUSTER] = (count, first_at - manager.SLOW_VALIDATION_SPACING - 1)
    assert manager.get_client(CLUSTER) is None
    assert reconnects == [CLUSTER]
    assert CLUSTER not in manager._slow_validations, "eviction clears the tolerance"


def test_eviction_and_a_good_probe_both_reset_the_tolerance(manager):
    client = _Client([requests.exceptions.ReadTimeout("slow")])
    manager._clients[CLUSTER] = client
    manager._auto_reconnect = lambda cid: None  # type: ignore[assignment]
    assert manager.get_client(CLUSTER) is client
    assert CLUSTER in manager._slow_validations
    manager.cleanup_connection(CLUSTER)
    assert CLUSTER not in manager._slow_validations


def test_a_successful_probe_resets_the_tolerance(manager):
    client = _Client([requests.exceptions.ReadTimeout("slow"), None, requests.exceptions.ReadTimeout("slow")])
    manager._clients[CLUSTER] = client
    manager._auto_reconnect = lambda cid: None  # type: ignore[assignment]
    assert manager.get_client(CLUSTER) is client
    assert manager.get_client(CLUSTER) is client
    assert CLUSTER not in manager._slow_validations, "a good probe clears the tolerance"
    assert manager.get_client(CLUSTER) is client


def test_a_connect_timeout_still_evicts_at_once(manager):
    client = _Client([requests.exceptions.ConnectTimeout("no route")])
    manager._clients[CLUSTER] = client
    reconnects = []
    manager._auto_reconnect = lambda cid: reconnects.append(cid) or None  # type: ignore[assignment]
    assert manager.get_client(CLUSTER) is None
    assert reconnects == [CLUSTER]


# --- the reconnected session -----------------------------------------------------------------

def test_an_auto_reconnected_client_carries_the_configured_session(manager, monkeypatch):
    class _Session:
        def __init__(self):
            self.adapters = {}

        def mount(self, prefix, adapter):
            self.adapters[prefix] = adapter

        def get_adapter(self, url):
            return self.adapters.get("https://")

    class _Api:
        def __init__(self):
            self.session = _Session()

        def get(self, timeout=None):
            return types.SimpleNamespace(status_code=200, json=lambda: {"metadata": {"auth": "trusted"}})

    class _Client:
        def __init__(self, *a, **k):
            self.api = _Api()
            self.host_info = {}

    monkeypatch.setattr("app.services.lxd.connection.pylxd.Client", _Client)
    manager._create_temp_cert_files = lambda cid, c, k: ("/tmp/c", "/tmp/k")  # type: ignore[assignment]
    manager._connection_info[CLUSTER] = {"endpoint": "https://x:8443", "cert_content": "c", "key_content": "k", "verify": False}

    client = manager._auto_reconnect(CLUSTER)
    assert client is not None
    assert isinstance(client.api.session.get_adapter("https://x"), TimeoutHTTPAdapter)
