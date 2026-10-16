"""A project relocation recreates each profile with its networks and root pool decided."""

from __future__ import annotations

from typing import Any

from app.services.migrations.profile_clone import clone_profile, clone_profiles, profile_fingerprint

USABLE = ["lxdbr0", "lxdbr1", "parent:enp5s0"]
POOLS = ["default", "alt"]


def _profile(name: str = "web", **devices: Any) -> dict[str, Any]:
    return {
        "name": name,
        "description": "Web tier",
        "config": {"limits.cpu": "2", "user.note": "kept"},
        "devices": devices,
    }


def _clone(profile: dict[str, Any], *, mapping: dict[str, str] | None = None, root: str | None = None) -> Any:
    return clone_profile(
        profile,
        network_mapping=mapping or {},
        usable_networks=USABLE,
        pools=POOLS,
        root_pool_mapping=root,
    )


def test_a_profile_keeps_its_config_and_same_named_network_and_pool() -> None:
    clone = _clone(
        _profile(
            eth0={"type": "nic", "network": "lxdbr0", "name": "eth0"},
            root={"type": "disk", "path": "/", "pool": "default", "size": "8GiB"},
        )
    )

    assert clone.findings == []
    assert clone.config == {"limits.cpu": "2", "user.note": "kept"}
    assert clone.devices["eth0"] == {"type": "nic", "network": "lxdbr0", "name": "eth0"}
    assert clone.devices["root"]["pool"] == "default"
    assert clone.root_pool == "default"
    assert [decision.via for decision in clone.nic_decisions] == ["same_name"]


def test_a_mapping_rewrites_the_nic_and_root_pool() -> None:
    clone = _clone(
        _profile(
            eth0={"type": "nic", "network": "lxdbr0", "name": "eth0"},
            root={"type": "disk", "path": "/", "pool": "default"},
        ),
        mapping={"lxdbr0": "lxdbr1"},
        root="alt",
    )

    assert clone.findings == []
    assert clone.devices["eth0"]["network"] == "lxdbr1"
    assert clone.devices["root"]["pool"] == "alt"
    assert clone.nic_decisions[0].as_dict() == {
        "device": "eth0",
        "source": "lxdbr0",
        "target": "lxdbr1",
        "via": "mapping",
    }


def test_an_unusable_network_or_pool_is_a_finding_never_a_guess() -> None:
    clone = clone_profile(
        _profile(
            eth0={"type": "nic", "network": "proj-net", "name": "eth0"},
            eth1={"type": "nic", "network": "lxdbr0"},
            root={"type": "disk", "path": "/", "pool": "fast"},
        ),
        network_mapping={"lxdbr0": "other-project-net"},
        usable_networks=USABLE,
        pools=POOLS,
        root_pool_mapping=None,
    )

    codes = [code for code, _message, _fix in clone.findings]
    assert codes.count("network_mapping_required") == 1
    assert codes.count("network_mapping_invalid") == 1
    assert "target_pool_mapping_required" in codes
    # The unresolved values stay as they were so nothing is invented for the target.
    assert clone.devices["eth0"]["network"] == "proj-net"
    assert clone.devices["root"]["pool"] == "fast"


def test_a_mapped_pool_that_does_not_exist_is_refused() -> None:
    clone = _clone(_profile(root={"type": "disk", "path": "/", "pool": "default"}), root="missing")

    assert [code for code, _m, _f in clone.findings] == ["target_pool_missing"]


def test_host_bound_devices_and_raw_config_are_refused_like_on_an_instance() -> None:
    profile = _profile(
        gpu={"type": "gpu"},
        web={"type": "proxy", "listen": "tcp:0.0.0.0:80", "connect": "tcp:127.0.0.1:80"},
        data={"type": "disk", "path": "/data", "source": "/srv/data"},
        odd={"type": "mystery"},
    )
    profile["config"]["raw.lxc"] = "lxc.apparmor.profile=unconfined"

    codes = sorted(code for code, _m, _f in _clone(profile).findings)

    assert codes == [
        "attached_volume_or_host_path",
        "profile_raw_config",
        "unknown_or_unsupported_device",
        "unsupported_profile_device",
        "unsupported_profile_device",
    ]


def test_the_default_profile_is_cloned_first_and_mappings_apply_to_all() -> None:
    clones = clone_profiles(
        [
            _profile("zeta", eth0={"type": "nic", "network": "lxdbr0"}),
            _profile("default", root={"type": "disk", "path": "/", "pool": "default"}),
            _profile("alpha"),
        ],
        mappings={"networks": {"lxdbr0": "lxdbr1"}, "storage_pools": {"*": "alt"}},
        usable_networks=USABLE,
        pools=POOLS,
    )

    assert [clone.name for clone in clones] == ["default", "alpha", "zeta"]
    assert clones[0].devices["root"]["pool"] == "alt"
    assert clones[2].devices["eth0"]["network"] == "lxdbr1"


def test_the_fingerprint_follows_what_instances_inherit_not_the_description() -> None:
    first = _profile(eth0={"type": "nic", "network": "lxdbr0"})
    second = {**first, "description": "renamed"}

    assert profile_fingerprint(first) == profile_fingerprint(second)
    assert profile_fingerprint({**first, "config": {"limits.cpu": "4"}}) != profile_fingerprint(first)
    assert profile_fingerprint(None) is None
    # The clone's record reads back with the same fingerprint the plan approved.
    clone = _clone(first)
    assert profile_fingerprint(clone.record()) == clone.fingerprint
