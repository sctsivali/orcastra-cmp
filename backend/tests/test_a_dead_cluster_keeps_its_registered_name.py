"""A cluster that stops answering must not also lose its name and its type.

`_check_single_node_status` reads the registry metadata (endpoint, display name, type) on every
path. Read only on the healthy path, an unreachable cluster would come back as its raw id with
no type and endpoint="unknown". `/nodes` feeds the availability projection, which feeds every
cluster picker in the app, so the clusters an operator most needs to identify would be the ones
showing a raw id and a bare "Hypervisor".
"""

from types import SimpleNamespace

import pytest

from app.models.schemas import NodeStatus
from app.services.lxd import health
from app.services.lxd.lxd_service import LXDService

SETTINGS = SimpleNamespace(vault_clusters_path="secret/data/clusters")

REGISTERED = {
    "bliss001": {"display_name": "BSD Lab", "endpoint": "https://10.9.8.49:9443", "type": "LXD"},
    "orin": {"display_name": "Jetson Orin", "endpoint": "https://orin:8443", "type": "Incus"},
    "bare": {"endpoint": "https://bare:8443"},
}


def _service(fail=()):
    def vault_read(path):
        cluster_id = path.split("/")[-2]
        if cluster_id in fail:
            raise RuntimeError("vault is sealed")
        return dict(REGISTERED[cluster_id])

    service = SimpleNamespace(
        cert_manager=SimpleNamespace(vault=SimpleNamespace(vault_read=vault_read))
    )
    # The stub stands in for `self`, so the real method has to be bound onto it rather than
    # reimplemented here: a copy would pass while the real one breaks.
    service._read_registry = lambda name, settings: LXDService._read_registry(service, name, settings)
    return service


def _registry(name, fail=()):
    return LXDService._read_registry(_service(fail), name, SETTINGS)


def test_the_registration_is_read_whatever_the_cluster_is_doing():
    assert _registry("bliss001") == {
        "endpoint": "https://10.9.8.49:9443",
        "display_name": "BSD Lab",
        "node_type": "lxd",
        "clustered": None,
    }


def test_the_type_is_normalised_but_only_when_one_was_recorded():
    """normalize_node_type(None) answers "lxd", which is the default this exists to remove."""
    assert _registry("orin")["node_type"] == "incus"
    assert _registry("bare")["node_type"] is None
    assert _registry("bare")["display_name"] is None


def test_an_unreadable_registration_says_so_rather_than_guessing():
    unknown = _registry("bliss001", fail={"bliss001"})
    assert unknown == {
        "endpoint": "unknown",
        "display_name": None,
        "node_type": None,
        "clustered": None,
    }


def test_the_clustering_state_rides_along_on_a_read_that_already_happens():
    """A clustered host that is down keeps its Clustered marker.

    /clusters and /nodes both fall back to the stored value, so the clusters table and the
    dashboard node card agree about the same offline host. The record is read here on every path,
    before the breaker check.

    Only a bool is a reading. A record without this key has no opinion, and a legacy string must
    not be coerced into one.
    """
    REGISTERED["clustered-host"] = {
        "endpoint": "https://10.9.8.60:8443",
        "display_name": "Clustered Host",
        "type": "MicroCloud",
        "clustered": True,
    }
    REGISTERED["stringly"] = dict(REGISTERED["clustered-host"], clustered="true")
    try:
        assert _registry("clustered-host")["clustered"] is True
        assert _registry("stringly")["clustered"] is None
        assert _registry("bliss001")["clustered"] is None
    finally:
        del REGISTERED["clustered-host"]
        del REGISTERED["stringly"]


@pytest.fixture
def tripped():
    health.record_failure("bliss001", "connection refused")
    yield
    health.clear("bliss001")


def test_a_breaker_tripped_cluster_still_carries_its_name_and_type(tripped):
    """The picker shows "BSD Lab", not the raw id `bliss001`."""
    status = LXDService._check_single_node_status(_service(), "bliss001", SETTINGS)

    assert isinstance(status, NodeStatus)
    assert status.connected is False
    assert status.display_name == "BSD Lab"
    assert status.node_type == "lxd"
    assert status.endpoint == "https://10.9.8.49:9443"


def test_a_cluster_with_no_client_still_carries_its_name_and_type():
    service = _service()
    service.get_client_with_reconnect = lambda name: None
    health.clear("orin")
    status = LXDService._check_single_node_status(service, "orin", SETTINGS)

    assert status.connected is False
    assert status.display_name == "Jetson Orin"
    assert status.node_type == "incus"
    health.clear("orin")


def test_a_probe_that_raises_still_carries_its_name_and_type():
    def _raise(name):
        raise RuntimeError("host unreachable")

    service = _service()
    service.get_client_with_reconnect = _raise
    health.clear("orin")
    status = LXDService._check_single_node_status(service, "orin", SETTINGS)

    assert status.connected is False
    assert status.display_name == "Jetson Orin"
    assert status.node_type == "incus"
    assert "host unreachable" in (status.error or "")
    health.clear("orin")
