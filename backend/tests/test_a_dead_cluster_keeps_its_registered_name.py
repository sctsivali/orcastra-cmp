"""A cluster that stops answering must not also lose its name and its type.

`_check_single_node_status` already pulled the endpoint onto every path, with the reason written
at the call site: returning registry metadata only on the healthy path made every dead cluster
report endpoint="unknown". The display name and the type are the same kind of value and still had
the same defect, so on the live fleet two unreachable clusters registered as "BSD Lab" and
"C1-JKT" came back as `blitz001` and `central1-argon` with no type at all.

That reaches further than one screen. `/nodes` feeds the availability projection, which feeds
every cluster picker in the app, so the clusters an operator most needs to identify were the only
ones showing a raw id and a bare "Hypervisor".
"""

from types import SimpleNamespace

import pytest

from app.models.schemas import NodeStatus
from app.services.lxd import health
from app.services.lxd.lxd_service import LXDService

SETTINGS = SimpleNamespace(vault_clusters_path="secret/data/clusters")

REGISTERED = {
    "blitz001": {"display_name": "BSD Lab", "endpoint": "https://10.1.1.49:9443", "type": "LXD"},
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
    # reimplemented here: a copy would pass while the shipped one regressed.
    service._read_registry = lambda name, settings: LXDService._read_registry(service, name, settings)
    return service


def _registry(name, fail=()):
    return LXDService._read_registry(_service(fail), name, SETTINGS)


def test_the_registration_is_read_whatever_the_cluster_is_doing():
    assert _registry("blitz001") == {
        "endpoint": "https://10.1.1.49:9443",
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
    unknown = _registry("blitz001", fail={"blitz001"})
    assert unknown == {
        "endpoint": "unknown",
        "display_name": None,
        "node_type": None,
        "clustered": None,
    }


def test_the_clustering_state_rides_along_on_a_read_that_already_happens():
    """A clustered host that is down keeps its Clustered marker.

    /clusters gained a stored fallback for this; /nodes did not, so the clusters table and the
    dashboard node card disagreed about the same offline host. The record was already being read
    here on every path, before the breaker check, and the key was simply dropped on the way out.

    Only a bool is a reading. A record written before this key existed has no opinion, and a
    legacy string must not be coerced into one.
    """
    REGISTERED["clustered-host"] = {
        "endpoint": "https://10.1.1.60:8443",
        "display_name": "Clustered Host",
        "type": "MicroCloud",
        "clustered": True,
    }
    REGISTERED["stringly"] = dict(REGISTERED["clustered-host"], clustered="true")
    try:
        assert _registry("clustered-host")["clustered"] is True
        assert _registry("stringly")["clustered"] is None
        assert _registry("blitz001")["clustered"] is None
    finally:
        del REGISTERED["clustered-host"]
        del REGISTERED["stringly"]


@pytest.fixture
def tripped():
    health.record_failure("blitz001", "connection refused")
    yield
    health.clear("blitz001")


def test_a_breaker_tripped_cluster_still_carries_its_name_and_type(tripped):
    """The defect in one sentence: the picker showed `blitz001`, not "BSD Lab"."""
    status = LXDService._check_single_node_status(_service(), "blitz001", SETTINGS)

    assert isinstance(status, NodeStatus)
    assert status.connected is False
    assert status.display_name == "BSD Lab"
    assert status.node_type == "lxd"
    assert status.endpoint == "https://10.1.1.49:9443"


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
