"""Managed-network overrides are validated before any daemon mutation."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.routers.networks import _validate_network_acls
from app.services.network_capabilities import network_capabilities, validate_network_capabilities
from app.services.network_config import NetworkCreateRequest, validate_network_config


def create(config, kind="bridge"):
    return NetworkCreateRequest(name="qa-network", type=kind, config=config)


@pytest.mark.parametrize("address", ["::/64", "fd42::1/64", "2001:db8:1:2:3:4:5:6/64", "::ffff:192.0.2.1/128"])
def test_complete_ipv6_addresses_and_compression_are_accepted(address):
    assert create({"ipv6.address": address}).config["ipv6.address"] == address


@pytest.mark.parametrize("address", ["1:2/64", "1:2:3:4:5:6:7/64", "1:2:3:4:5:6:7:8::/64",
                                     "fd42:::1/64", "::1/129", "fe80::1%eth0/64", "192.0.2.1/24", "fd42::1"])
def test_invalid_ipv6_addresses_are_rejected(address):
    with pytest.raises(ValidationError):
        create({"ipv6.address": address})


@pytest.mark.parametrize("key", ["raw.dnsmasq", "bridge.external_interfaces", "tunnel.peer.protocol", "user.arbitrary"])
@pytest.mark.parametrize("kind", ["bridge", "ovn"])
def test_dangerous_or_unknown_keys_remain_blocked(key, kind):
    with pytest.raises(ValidationError, match="Unsupported network config"):
        create({key: "anything"}, kind)


@pytest.mark.parametrize("config", [{"bridge.mtu": "1500"}, {"dns.mode": "managed"}, {"ipv6.dhcp.ranges": "fd42::10-fd42::20"}])
def test_bridge_only_overrides_are_not_sent_to_ovn(config):
    with pytest.raises(ValidationError, match="Unsupported network config"):
        create(config, "ovn")


@pytest.mark.parametrize("kind", ["physical", "macvlan", "sriov"])
def test_creation_stays_limited_to_the_two_supported_types(kind):
    with pytest.raises(ValidationError):
        create({}, kind)


def test_default_configuration_stays_unset():
    assert create({}).config == {}


def test_an_existing_uplink_can_use_interface_name_punctuation():
    assert create({"network": "existing_uplink.10"}, "ovn").config["network"] == "existing_uplink.10"


def test_rich_bridge_configuration_preserves_explicit_overrides():
    config = {
        "bridge.mtu": "1500", "ipv4.address": "10.42.0.1/24", "ipv4.nat": "true",
        "ipv4.dhcp": "true", "ipv4.dhcp.ranges": "10.42.0.10-10.42.0.30,10.42.0.40-10.42.0.50",
        "ipv6.address": "fd42::1/64", "ipv6.dhcp": "true", "ipv6.dhcp.stateful": "true",
        "ipv6.dhcp.ranges": "fd42::10-fd42::100", "dns.domain": "qa.example", "dns.mode": "managed",
        "security.acls": "trusted, isolated",
    }
    expected = {**config, "security.acls": "trusted,isolated"}
    assert create(config).config == expected


@pytest.mark.parametrize("value", ["yes", "0", "TRUE", "enabled", ""])
@pytest.mark.parametrize("key", ["ipv4.nat", "ipv6.dhcp", "ipv6.dhcp.stateful"])
def test_boolean_values_are_unambiguous(key, value):
    with pytest.raises(ValidationError):
        create({key: value})


@pytest.mark.parametrize("mtu", ["0", "67", "65536", "1500.5", "-1500", "NaN", "１２８０"])
def test_mtu_must_be_a_bounded_integer(mtu):
    with pytest.raises(ValidationError, match="MTU"):
        create({"bridge.mtu": mtu})


def test_small_mtu_is_only_accepted_when_ipv6_is_disabled():
    with pytest.raises(ValidationError, match="1280"):
        create({"bridge.mtu": "1200"})
    assert create({"bridge.mtu": "1200", "ipv6.address": "none"}).config["bridge.mtu"] == "1200"


@pytest.mark.parametrize("ranges", ["10.42.1.10-10.42.1.20", "10.42.0.20-10.42.0.10",
    "10.42.0.0-10.42.0.20", "10.42.0.200-10.42.0.255", "10.42.0.1-10.42.0.5",
    "10.42.0.10-10.42.0.20,10.42.0.15-10.42.0.30", "10.42.0.10", "fd42::10-fd42::20"])
def test_dhcp_ranges_cannot_overlap_or_escape_the_custom_subnet(ranges):
    with pytest.raises(ValidationError):
        create({"ipv4.address": "10.42.0.1/24", "ipv4.dhcp.ranges": ranges})


def test_ipv6_ranges_require_enabled_stateful_dhcp():
    for extra in ({}, {"ipv6.dhcp.stateful": "false"}, {"ipv6.dhcp": "false", "ipv6.dhcp.stateful": "true"}):
        with pytest.raises(ValidationError, match="DHCP"):
            create({"ipv6.dhcp.ranges": "fd42::10-fd42::20", **extra})


def test_ranges_require_an_explicit_subnet_instead_of_a_random_auto_subnet():
    with pytest.raises(ValidationError, match="custom CIDR"):
        create({"ipv4.dhcp.ranges": "10.42.0.10-10.42.0.20"})


@pytest.mark.parametrize("key", ["ipv4.nat", "ipv4.dhcp", "ipv4.dhcp.ranges"])
def test_disabled_address_family_cannot_carry_service_overrides(key):
    with pytest.raises(ValidationError, match="enabled"):
        create({"ipv4.address": "none", key: "true" if key != "ipv4.dhcp.ranges" else "10.0.0.10-10.0.0.20"})


@pytest.mark.parametrize("domain", ["bad domain", "-bad.example", "bad..example", "a" * 64 + ".example", "."])
def test_dns_domains_cannot_carry_invalid_labels(domain):
    with pytest.raises(ValidationError, match="DNS domain"):
        create({"dns.domain": domain})


@pytest.mark.parametrize("names", ["allow,allow", "../outside", "valid,", "a" * 64, "valid,1invalid"])
def test_acl_identifiers_are_safe_unique_and_bounded(names):
    with pytest.raises(ValidationError):
        create({"security.acls": names})


def test_an_update_can_remove_optional_overrides():
    assert validate_network_config({"bridge.mtu": "", "dns.mode": ""}, "bridge", allow_unset=True) == {
        "bridge.mtu": "", "dns.mode": "",
    }


@pytest.mark.parametrize("platform", ["lxd", "incus", "microcloud"])
def test_capabilities_follow_extensions_rather_than_platform_names(platform):
    client = SimpleNamespace(host_info={"environment": {"server": platform}, "api_extensions": [
        "network_type_ovn", "network_ovn_nat", "network_ovn_dhcp", "network_acl", "network_bridge_acl", "network_ovn_acl",
    ]})
    capabilities = network_capabilities(client)
    assert capabilities.known
    assert capabilities.types == ["bridge", "ovn"]
    assert "security.acls" in capabilities.config_keys["bridge"]
    assert "ipv6.dhcp.ranges" not in capabilities.config_keys["ovn"]
    validate_network_capabilities(client, "ovn", {"network": "uplink", "ipv4.dhcp": "false"})


def test_advertised_missing_features_are_denied_before_submission():
    client = SimpleNamespace(host_info={"api_extensions": ["network_type_ovn"]})
    for kind, config in (("bridge", {"security.acls": "allow"}), ("ovn", {"ipv4.nat": "true"}),
                         ("ovn", {"ipv4.dhcp": "false"})):
        with pytest.raises(HTTPException) as error:
            validate_network_capabilities(client, kind, config)
        assert error.value.status_code == 400
    with pytest.raises(HTTPException):
        validate_network_capabilities(SimpleNamespace(host_info={"api_extensions": []}), "ovn", {})


@pytest.mark.parametrize(("dhcp", "ranges"), [(False, False), (True, False), (False, True), (True, True)])
def test_ovn_dhcp_controls_and_ranges_use_independent_extensions(dhcp, ranges):
    extensions = ["network_type_ovn"]
    if dhcp:
        extensions.append("network_ovn_dhcp")
    if ranges:
        extensions.append("ovn_dhcp_ranges")
    client = SimpleNamespace(host_info={"api_extensions": extensions})
    keys = network_capabilities(client).config_keys["ovn"]
    assert ("ipv4.dhcp" in keys) is dhcp
    assert ("ipv6.dhcp" in keys) is dhcp
    assert ("ipv4.dhcp.ranges" in keys) is ranges
    overrides = {"ipv4.address": "10.42.0.1/24", "ipv4.dhcp.ranges": "10.42.0.10-10.42.0.20"}
    if ranges:
        validate_network_capabilities(client, "ovn", overrides)
    else:
        with pytest.raises(HTTPException) as error:
            validate_network_capabilities(client, "ovn", overrides)
        assert error.value.status_code == 400
        assert "ipv4.dhcp.ranges" in error.value.detail


def test_missing_extension_metadata_preserves_daemon_fallback():
    client = SimpleNamespace(host_info={})
    assert not network_capabilities(client).known
    validate_network_capabilities(client, "ovn", {"network": "uplink"})


def test_acl_discovery_uses_the_resolved_owner_and_refuses_another_projects_acl(monkeypatch):
    calls = []

    def scoped_list(client, collection, project):
        calls.append((collection, project))
        return [{"name": "qa-allow"}]

    monkeypatch.setattr("app.routers.networks.list_in_project", scoped_list)
    _validate_network_acls(object(), {"security.acls": "qa-allow"}, "org-qa")
    with pytest.raises(HTTPException) as error:
        _validate_network_acls(object(), {"security.acls": "neighbor-only"}, "org-qa")
    assert error.value.status_code == 400
    assert calls == [("network-acls", "org-qa"), ("network-acls", "org-qa")]
