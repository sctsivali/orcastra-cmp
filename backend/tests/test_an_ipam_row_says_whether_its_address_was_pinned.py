"""
An IPAM row says whether its address was pinned on the device or handed out dynamically.

A reconciliation against a corporate IPAM needs to tell a reservation from a lease. The rule
the instances page applies (a NIC device carrying `ipv4.address` / `ipv6.address`) is applied
to each allocation, read from the instance's expanded devices so a profile-inherited pin
counts, and compared as parsed addresses so a prefix or IPv6 compression cannot hide a match.
"""

from app.services.lxd.ipam_join import classify, network_facts
from app.services.lxd.project_scope import InstanceRecord


def _record(devices, project="default", name="web1") -> InstanceRecord:
    return InstanceRecord({"name": name, "project": project, "expanded_devices": devices})


NIC = {"type": "nic", "network": "ovn-a", "ipv4.address": "192.168.3.50"}


def test_a_pin_on_the_instances_own_nic_is_static() -> None:
    rec = _record({"eth0": {**NIC}})
    assert classify("instance", "192.168.3.50", rec, "ovn-a", True) == "static"


def test_a_pin_inherited_from_a_profile_counts_too() -> None:
    # expanded_devices already merges the profile's NIC; the own `devices` map is empty
    rec = InstanceRecord({"name": "web1", "devices": {}, "expanded_devices": {"eth0": {**NIC}}})
    assert classify("instance", "192.168.3.50", rec, "ovn-a", True) == "static"


def test_a_pin_on_another_address_leaves_this_one_dhcp() -> None:
    rec = _record({"eth0": {**NIC}})
    assert classify("instance", "192.168.3.51", rec, "ovn-a", True) == "dhcp"


def test_no_pin_is_dhcp() -> None:
    rec = _record({"eth0": {"type": "nic", "network": "ovn-a"}})
    assert classify("instance", "192.168.3.50", rec, "ovn-a", True) == "dhcp"


def test_the_prefix_and_ipv6_compression_do_not_hide_a_match() -> None:
    rec = _record({"eth0": {"type": "nic", "network": "ovn-a", "ipv6.address": "fd42:0:0::10"}})
    assert classify("instance", "fd42::10/64", rec, "ovn-a", True) == "static"
    rec4 = _record({"eth0": {**NIC}})
    assert classify("instance", "192.168.3.50/24", rec4, "ovn-a", True) == "static"


def test_a_v4_row_is_never_matched_by_a_v6_pin() -> None:
    rec = _record({"eth0": {"type": "nic", "network": "ovn-a", "ipv6.address": "fd42::10"}})
    assert classify("instance", "192.168.3.50", rec, "ovn-a", True) == "dhcp"


def test_a_nic_that_names_its_network_as_parent_is_matched_by_address() -> None:
    # A bridged NIC names the bridge in `parent` instead of `network`, so the address decides.
    rec = _record({"eth1": {"type": "nic", "nictype": "bridged", "parent": "lxdbr0",
                            "ipv4.address": "10.22.114.231"}})
    assert classify("instance", "10.22.114.231", rec, "lxdbr0", True) == "static"


def test_the_gateway_forward_and_load_balancer_rows_keep_their_kind() -> None:
    assert classify("network", "192.168.3.1", None, "ovn-a", True) == "gateway"
    assert classify("network-forward", "10.0.0.9", None, "ovn-a", True) == "forward"
    assert classify("network-load-balancer", "10.0.0.8", None, "ovn-a", True) == "load_balancer"


def test_an_unread_instance_list_is_unknown_never_a_silent_dhcp() -> None:
    rec = _record({"eth0": {**NIC}})
    assert classify("instance", "192.168.3.50", rec, "ovn-a", False) == "unknown"
    assert classify("instance", "192.168.3.50", None, "ovn-a", True) == "unknown"


def test_a_row_of_a_kind_lxd_has_not_taught_us_is_unknown() -> None:
    assert classify("something-new", "10.0.0.1", None, "ovn-a", True) == "unknown"


CONFIGS = {
    "default": {},
    "isolated": {"features.networks": "true"},
    "shared": {"features.networks": "false"},
    "unset": {},
}
NETWORKS = {
    ("default", "lxdbr0"): {"type": "bridge", "config": {"ipv4.address": "10.22.114.1/24", "ipv6.address": "none"}},
    ("isolated", "ovn-a"): {"type": "ovn", "config": {"ipv4.address": "192.168.3.1/24", "ipv6.address": "fd42::1/64"}},
    ("default", "enp86s0"): {"type": "physical", "config": {}},
}


def test_network_facts_resolve_the_owning_project() -> None:
    assert network_facts("ovn-a", "isolated", CONFIGS, NETWORKS) == ("ovn", "isolated", "192.168.3.1/24", "fd42::1/64")
    # A project without features.networks uses default's bridge.
    assert network_facts("lxdbr0", "shared", CONFIGS, NETWORKS) == ("bridge", "default", "10.22.114.1/24", "")
    assert network_facts("lxdbr0", "unset", CONFIGS, NETWORKS) == ("bridge", "default", "10.22.114.1/24", "")


def test_network_facts_treat_none_and_auto_as_no_subnet() -> None:
    nets = {("default", "b"): {"type": "bridge", "config": {"ipv4.address": "auto", "ipv6.address": "none"}}}
    assert network_facts("b", "default", CONFIGS, nets) == ("bridge", "default", "", "")


def test_an_unmanaged_physical_network_has_a_type_and_no_subnet() -> None:
    assert network_facts("enp86s0", "default", CONFIGS, NETWORKS) == ("physical", "default", "", "")


def test_a_network_that_is_gone_answers_empty_facts() -> None:
    assert network_facts("vanished", "default", CONFIGS, NETWORKS) == ("", "", "", "")
