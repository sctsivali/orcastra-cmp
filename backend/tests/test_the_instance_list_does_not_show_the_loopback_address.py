"""
The IP column shows the addresses an operator can reach and hides loopback addresses.

Skipping `lo` by name is not enough: a loopback address can sit on another interface. One
predicate decides for all three extractors: an address is shown when it is a global (or
host-routable) address, not loopback, not link-local, not scope local.
"""

from types import SimpleNamespace
from typing import Any, Dict

from app.services.lxd import utils as lxd_utils


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
        # A loopback address that is not on lo: skipping by name alone would keep it.
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
    # A state without a scope field (older daemons) still hides loopback and link-local.
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
    # The list reads state inline (recursion=2); the row mapper must hide the same addresses.
    from app.services.lxd.instance import _vm_info_from_row

    row = {"name": "web1", "status": "Running", "status_code": 103, "project": "default", "state": STATE}
    assert _vm_info_from_row(row, "nuc2", "").ip_addresses == ["10.0.0.5", "2001:db8::5"]
    stopped = {"name": "web2", "status": "Stopped", "status_code": 102, "project": "default", "state": {"network": None}}
    assert _vm_info_from_row(stopped, "nuc2", "").ip_addresses == []
