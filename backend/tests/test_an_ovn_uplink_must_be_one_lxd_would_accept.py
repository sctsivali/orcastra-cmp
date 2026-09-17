"""An OVN network can only be uplinked to a managed bridge or physical network.

The create form offered every interface the node reported and the API forwarded
whatever came back, so an operator could pick another OVN network, a macvlan interface or the
loopback as an OVN uplink. LXD refuses all of those, and the refusal arrived late enough that
operators saw an instance stuck in Creating with no reason shown.

The candidate list here is the real inventory of one LXD 6.9 host as captured on 2026-09-09: 23 networks,
of which 11 are unmanaged, loopback or of a type LXD does not accept. That is the shape the
partner's dropdown had too.
"""

import pytest
from fastapi import HTTPException

from app.routers.networks import NetworkCreateRequest, validate_uplink


# Taken from `GET /1.0/networks?recursion=1` on nuc1 (sivali1), LXD 6.9.
NUC1_NETWORKS = {
    "acmebr0": {"type": "bridge", "managed": True},
    "c2fs-mgmt": {"type": "bridge", "managed": True},
    "c2fs-uplink": {"type": "bridge", "managed": True},
    "fj-br1": {"type": "bridge", "managed": True},
    "fj-br2": {"type": "bridge", "managed": True},
    "lxdbr0": {"type": "bridge", "managed": True},
    "lxdbr1": {"type": "bridge", "managed": True},
    "micronet1": {"type": "bridge", "managed": True},
    "micronet2": {"type": "bridge", "managed": True},
    "rull_pool": {"type": "bridge", "managed": True},
    "ryobr0": {"type": "bridge", "managed": True},
    "ryobr1": {"type": "bridge", "managed": True},
    "lo": {"type": "loopback", "managed": False},
    "enp86s0": {"type": "physical", "managed": False},
    "br-06657ec224f5": {"type": "bridge", "managed": False},
    "docker0": {"type": "bridge", "managed": False},
    "macc204c041": {"type": "unknown", "managed": False},
    "tailscale0": {"type": "unknown", "managed": False},
    "tap8d1a7464": {"type": "unknown", "managed": False},
}

# A MicroCloud's shape: a real uplink bridge beside networks that cannot be one.
FIELD_NETWORKS = {
    "br-uplink": {"type": "bridge", "managed": True},
    "uplink": {"type": "physical", "managed": True},
    "ovn-production": {"type": "ovn", "managed": True},
    "ovn-sealion": {"type": "ovn", "managed": True},
    "mv-mgmt": {"type": "macvlan", "managed": False},
    "lo": {"type": "loopback", "managed": False},
}


def ovn(uplink=None, name="test"):
    config = {"network": uplink} if uplink is not None else {}
    return NetworkCreateRequest(name=name, type="ovn", config=config)


def test_a_managed_bridge_is_accepted():
    validate_uplink(ovn("br-uplink"), FIELD_NETWORKS)


def test_a_managed_physical_network_is_accepted():
    validate_uplink(ovn("uplink"), FIELD_NETWORKS)


@pytest.mark.parametrize("uplink", ["ovn-production", "ovn-sealion"])
def test_another_ovn_network_is_refused(uplink):
    """The circular dependency: OVN cannot uplink to OVN."""
    with pytest.raises(HTTPException) as exc:
        validate_uplink(ovn(uplink), FIELD_NETWORKS)
    assert exc.value.status_code == 400
    assert "ovn" in exc.value.detail


@pytest.mark.parametrize("uplink", ["lo", "mv-mgmt"])
def test_loopback_and_macvlan_are_refused(uplink):
    with pytest.raises(HTTPException) as exc:
        validate_uplink(ovn(uplink), FIELD_NETWORKS)
    assert exc.value.status_code == 400


def test_an_unmanaged_bridge_is_refused_even_though_its_type_is_right():
    with pytest.raises(HTTPException) as exc:
        validate_uplink(ovn("docker0"), NUC1_NETWORKS)
    assert "not managed" in exc.value.detail


def test_a_missing_uplink_is_refused_before_the_daemon_sees_it():
    with pytest.raises(HTTPException) as exc:
        validate_uplink(ovn(), FIELD_NETWORKS)
    assert "needs an uplink" in exc.value.detail


def test_a_name_that_does_not_exist_is_refused():
    with pytest.raises(HTTPException) as exc:
        validate_uplink(ovn("ovn-uat-hermes"), FIELD_NETWORKS)
    assert "does not exist" in exc.value.detail


def test_the_refusal_names_the_uplinks_that_would_work():
    """A rejection that does not say what to pick instead just moves the guessing."""
    with pytest.raises(HTTPException) as exc:
        validate_uplink(ovn("lo"), FIELD_NETWORKS)
    assert "br-uplink" in exc.value.detail
    assert "uplink" in exc.value.detail


def test_a_node_with_no_usable_uplink_says_so_rather_than_listing_nothing():
    only_bad = {"lo": {"type": "loopback", "managed": False}}
    with pytest.raises(HTTPException) as exc:
        validate_uplink(ovn("lo"), only_bad)
    assert "No network on this node can serve as an uplink" in exc.value.detail


def test_every_candidate_nuc1_offers_today_is_judged_the_way_lxd_would():
    """11 of nuc1's 23 networks cannot be an uplink. Each one must be refused."""
    usable, refused = [], []
    for name in NUC1_NETWORKS:
        try:
            validate_uplink(ovn(name), NUC1_NETWORKS)
            usable.append(name)
        except HTTPException:
            refused.append(name)
    assert set(refused) == {
        "lo",
        "enp86s0",
        "br-06657ec224f5",
        "docker0",
        "macc204c041",
        "tailscale0",
        "tap8d1a7464",
    }
    assert "lxdbr0" in usable and "c2fs-uplink" in usable


def test_a_bridge_create_may_not_carry_an_ovn_uplink_key():
    request = NetworkCreateRequest(
        name="br0", type="bridge", config={"network": "br-uplink"}
    )
    with pytest.raises(HTTPException) as exc:
        validate_uplink(request, FIELD_NETWORKS)
    assert "only applies to an OVN network" in exc.value.detail


def test_a_plain_bridge_create_is_untouched():
    validate_uplink(
        NetworkCreateRequest(name="br0", type="bridge", config={"ipv4.nat": "true"}),
        FIELD_NETWORKS,
    )


class TestRestrictedUplinks:
    """`restricted.networks.uplinks` only binds when the project is restricted at all."""

    RESTRICTED = {
        "restricted": "true",
        "restricted.networks.uplinks": "br-uplink",
    }

    def test_a_restricted_project_may_only_use_the_uplinks_it_names(self):
        validate_uplink(ovn("br-uplink"), FIELD_NETWORKS, self.RESTRICTED)
        with pytest.raises(HTTPException) as exc:
            validate_uplink(ovn("uplink"), FIELD_NETWORKS, self.RESTRICTED)
        assert "restricts uplinks" in exc.value.detail

    def test_the_list_is_ignored_when_the_project_is_not_restricted(self):
        """LXD ignores the whole restricted.* family unless `restricted` is true, and most
        projects on this fleet set it to false explicitly. Honouring the list regardless
        would narrow the choice for nearly every project."""
        config = {"restricted": "false", "restricted.networks.uplinks": "br-uplink"}
        validate_uplink(ovn("uplink"), FIELD_NETWORKS, config)

    def test_a_restricted_project_with_no_list_is_not_narrowed(self):
        validate_uplink(ovn("uplink"), FIELD_NETWORKS, {"restricted": "true"})

    def test_the_offer_shown_to_a_restricted_project_is_the_restricted_one(self):
        with pytest.raises(HTTPException) as exc:
            validate_uplink(ovn("lo"), FIELD_NETWORKS, self.RESTRICTED)
        assert "br-uplink" in exc.value.detail
        assert "uplink," not in exc.value.detail


def test_both_write_paths_validate_the_uplink():
    """A ratchet, because create was guarded and update was not.

    `network` is in ALLOWED_NETWORK_CONFIG_KEYS, so PUT /networks/{name} could set an OVN
    uplink LXD refuses while POST /networks refused the identical value. Guarding only the
    create left the defect reachable through the other door.
    """
    import ast
    import pathlib

    router = pathlib.Path(__file__).resolve().parents[1] / "app" / "routers" / "networks.py"
    tree = ast.parse(router.read_text())
    writers = {"create_network", "update_network"}
    seen = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in writers:
            seen[node.name] = "validate_uplink" in ast.unparse(node)
    missing_handler = writers - set(seen)
    assert not missing_handler, f"handler not found, renamed? {missing_handler}"
    unguarded = sorted(name for name, ok in seen.items() if not ok)
    assert unguarded == [], f"these write an uplink without validating it: {unguarded}"
