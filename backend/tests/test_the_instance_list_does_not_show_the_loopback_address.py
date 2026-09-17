"""
The IP column shows the addresses an operator can reach, never the loopback.

The instance list's collector walked every interface in the state, including `lo`, and kept
every inet address, so `127.0.0.1` sat beside the real address of every running instance.
The two other extractors skipped `lo` by name but would still keep a loopback address on
another interface. One predicate now decides for all three: an address is shown when it is
a global (or host-routable) address, not loopback, not link-local, not scope local.
"""

from types import SimpleNamespace
from typing import Any, Dict

from app.services.lxd import utils as lxd_utils
from app.services.lxd.instance import InstanceManager

STATE: Dict[str, Any] = {
    "network": {
        "lo": {"addresses": [
            {"family": "inet", "address": "127.0.0.1", "netmask": "8", "scope": "local"},
            {"family": "inet6", "address": "::1", "netmask": "128", "scope": "local"},
        ]},
        "eth0": {"addresses": [
            {"family": "inet", "address": "10.0.0.5", "netmask": "24", "scope": "global"},
            {"family": "inet6", "address": "fe80::216:3eff:fe00:1", "netmask": "64", "scope": "link"},
            {"family": "inet6", "address": "2001:db8::5", "netmask": "64", "scope": "global"},
        ]},
        # a loopback address that is not on lo: the name skip alone would keep it
        "dummy0": {"addresses": [
            {"family": "inet", "address": "127.0.0.2", "netmask": "8", "scope": "local"},
        ]},
    }
}


def test_is_display_address_keeps_reachable_addresses_only() -> None:
    keep = lxd_utils.is_display_address
    assert keep({"family": "inet", "address": "10.0.0.5", "scope": "global"})
    assert keep({"family": "inet", "address": "192.168.3.50", "scope": "global"})
    assert keep({"family": "inet6", "address": "2001:db8::5", "scope": "global"})
    assert not keep({"family": "inet", "address": "127.0.0.1", "scope": "local"})
    assert not keep({"family": "inet", "address": "127.0.0.2", "scope": "global"})
    assert not keep({"family": "inet6", "address": "::1", "scope": "local"})
    assert not keep({"family": "inet6", "address": "fe80::1", "scope": "link"})
    assert not keep({"family": "inet", "address": "", "scope": "global"})
    assert not keep({"family": "packet", "address": "00:16:3e:00:00:01"})
    # a state without a scope field (older daemons) still hides loopback and link-local
    assert keep({"family": "inet", "address": "10.0.0.5"})
    assert not keep({"family": "inet", "address": "127.0.0.1"})
    assert not keep({"family": "inet6", "address": "fe80::1"})


def test_the_state_extractor_hides_loopback_wherever_it_lives() -> None:
    assert lxd_utils.extract_ip_addresses_from_state(STATE) == ["10.0.0.5", "2001:db8::5"]


def test_the_pylxd_extractor_agrees() -> None:
    instance = SimpleNamespace(
        name="web1",
        status="Running",
        state=lambda: SimpleNamespace(network=STATE["network"]),
    )
    assert lxd_utils.extract_ip_addresses(instance) == ["10.0.0.5", "2001:db8::5"]


def test_the_instance_list_collector_agrees() -> None:
    class _State:
        def get(self, params=None, timeout=None):
            return SimpleNamespace(json=lambda: {"metadata": STATE})

    class _Instances:
        def __getitem__(self, name):
            return SimpleNamespace(state=_State())

    client = SimpleNamespace(api=SimpleNamespace(instances=_Instances()))
    manager = InstanceManager(connection_manager=None, resource_manager=None)
    assert manager._fetch_single_instance_state(client, "web1", "default") == (
        "web1", ["10.0.0.5", "2001:db8::5"],
    )
