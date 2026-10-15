"""The definition a migrated instance is created with, and the state it must read back as."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any, Optional

import pytest

from app.services.migrations.project_policy import MIGRATION_JOB_MARKER
from app.services.migrations.target_contract import (
    TargetContract,
    build_contract,
    effective_root_pool,
    mismatches,
    preserved_identity_config,
    target_matches,
)

SOURCE_ROOT = {"type": "disk", "path": "/", "pool": "default", "size": "20GiB"}
SOURCE_NIC = {"type": "nic", "network": "lxdbr0", "name": "eth0"}
BR_TARGET = {"type": "nic", "network": "br-target", "name": "eth0"}
MACVLAN = {"type": "nic", "nictype": "macvlan", "parent": "enp5s0"}
TARGET_ROOT = {"type": "disk", "path": "/", "pool": "default"}
VOLATILE = {"volatile.eth0.hwaddr": "00:16:3e:aa:aa:01", "volatile.base_image": "abc"}


def _source(
    *,
    local_devices: Optional[dict[str, Any]] = None,
    profile_devices: Optional[dict[str, Any]] = None,
    local_config: Optional[dict[str, Any]] = None,
    profile_config: Optional[dict[str, Any]] = None,
    profiles: Sequence[str] = ("default",),
    volatile: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    local_devices = local_devices or {}
    if profile_devices is None:
        profile_devices = {"root": SOURCE_ROOT, "eth0": SOURCE_NIC}
    local = {**(local_config or {}), **(VOLATILE if volatile is None else volatile)}
    local[MIGRATION_JOB_MARKER] = "earlier-job"
    return {
        "profiles": list(profiles),
        "config": local,
        "devices": copy.deepcopy(local_devices),
        "expanded_config": {**(profile_config or {}), **local},
        "expanded_devices": copy.deepcopy({**profile_devices, **local_devices}),
    }


def _target(config: Optional[dict[str, Any]] = None, **devices: Any) -> dict[str, Any]:
    """One target profile named ``default``: a root on pool ``default`` plus ``devices``."""
    return {"default": {"config": dict(config or {}), "devices": {"root": TARGET_ROOT, **devices}}}


DEFAULT_TARGET = _target(eth0=SOURCE_NIC)


def _contract(
    source: Mapping[str, Any],
    *,
    target_profiles: Optional[Mapping[str, Any]] = None,
    profile_mapping: Optional[Mapping[str, str]] = None,
    network_mapping: Optional[Mapping[str, str]] = None,
    root_pool: Optional[str] = "fast",
    intent: str = "move",
    overrides: Optional[Mapping[str, Any]] = None,
    usable: tuple[str, ...] = ("lxdbr0",),
) -> TargetContract:
    return build_contract(
        source=source,
        target_profiles=DEFAULT_TARGET if target_profiles is None else target_profiles,
        profile_mapping=profile_mapping or {},
        network_mapping=network_mapping or {},
        usable_networks=list(usable),
        root_pool=root_pool,
        intent=intent,
        config_overrides=overrides,
    )


def _decision(contract: TargetContract, device: str) -> dict[str, Any]:
    return next(d.as_dict() for d in contract.nic_decisions if d.device == device)


def test_profiles_follow_the_mapping_and_missing_targets_are_named() -> None:
    contract = _contract(_source(profiles=("default", "gpu")), profile_mapping={"gpu": "gpu-b"})

    assert contract.profiles == ["default", "gpu-b"]
    assert contract.missing_profiles == ["gpu-b"]


def test_an_explicit_network_mapping_wins_over_the_target_profile_nic() -> None:
    contract = _contract(
        _source(),
        target_profiles=_target(eth0=BR_TARGET),
        network_mapping={"lxdbr0": "ovn-a"},
        usable=("lxdbr0", "ovn-a"),
    )

    assert _decision(contract, "eth0") == {
        "device": "eth0", "source": "lxdbr0", "target": "ovn-a", "via": "mapping",
    }
    assert contract.devices["eth0"] == {"type": "nic", "network": "ovn-a", "name": "eth0"}
    assert "eth0" in contract.pinned_devices


@pytest.mark.parametrize(
    ("nic", "mapping", "expected"),
    [
        (MACVLAN, {"parent:enp5s0": "parent:enp6s0"}, {**MACVLAN, "parent": "enp6s0"}),
        (MACVLAN, {"parent:enp5s0": "ovn-a"}, {"type": "nic", "network": "ovn-a"}),
        (
            {"type": "nic", "network": "lxdbr0"},
            {"lxdbr0": "parent:br0"},
            {"type": "nic", "parent": "br0", "nictype": "bridged"},
        ),
    ],
)
def test_a_mapping_keyed_by_network_or_parent_rewrites_the_nic_attachment(
    nic: dict[str, Any], mapping: dict[str, str], expected: dict[str, Any]
) -> None:
    contract = _contract(
        _source(local_devices={"eth1": nic}),
        network_mapping=mapping,
        usable=("lxdbr0", *mapping.values()),
    )

    assert contract.devices["eth1"] == expected
    assert _decision(contract, "eth1")["via"] == "mapping"


def test_a_mapping_to_a_network_the_target_project_cannot_use_is_refused() -> None:
    contract = _contract(_source(), network_mapping={"lxdbr0": "other-tenant-net"})

    assert _decision(contract, "eth0") == {
        "device": "eth0", "source": "lxdbr0", "target": None, "via": "unmapped",
    }
    assert contract.unmapped_networks == [
        {"device": "eth0", "source": "lxdbr0", "mapped": "other-tenant-net"}
    ]
    assert contract.devices.get("eth0", {}).get("network") != "other-tenant-net"


def test_an_inherited_nic_takes_the_same_named_nic_of_the_target_profile() -> None:
    contract = _contract(_source(), target_profiles=_target(eth0=BR_TARGET))

    assert _decision(contract, "eth0")["via"] == "target_profile"
    assert _decision(contract, "eth0")["target"] == "br-target"
    assert "eth0" not in contract.devices
    assert contract.expected_devices["eth0"]["network"] == "br-target"


def test_a_local_nic_keeps_its_usable_network_even_when_the_target_profile_differs() -> None:
    contract = _contract(
        _source(local_devices={"eth0": SOURCE_NIC}), target_profiles=_target(eth0=BR_TARGET)
    )

    assert _decision(contract, "eth0")["via"] == "same_name"
    assert contract.devices["eth0"]["network"] == "lxdbr0"


def test_an_inherited_nic_the_target_profile_lacks_is_pinned_on_its_usable_network() -> None:
    contract = _contract(_source(), target_profiles=_target())

    assert _decision(contract, "eth0")["via"] == "same_name"
    assert contract.devices["eth0"] == SOURCE_NIC
    assert "eth0" in contract.pinned_devices


@pytest.mark.parametrize(
    ("nic", "key", "via"),
    [({"type": "nic", "network": "private"}, "private", "unmapped"), ({"type": "nic"}, "none", "unattached")],
)
def test_a_nic_without_a_usable_target_is_kept_but_left_undecided(
    nic: dict[str, Any], key: str, via: str
) -> None:
    contract = _contract(_source(local_devices={"eth1": nic}))

    assert _decision(contract, "eth1") == {"device": "eth1", "source": key, "target": None, "via": via}
    unmapped = [{"device": "eth1", "source": key}] if via == "unmapped" else []
    assert contract.unmapped_networks == unmapped


def test_the_root_pool_is_pinned_on_the_root_device_and_keeps_its_size() -> None:
    pinned = _contract(_source())
    already_fast = _target(eth0=SOURCE_NIC, root={**SOURCE_ROOT, "pool": "fast"})
    rendered = _contract(_source(), target_profiles=already_fast)

    assert pinned.devices["root"] == {**SOURCE_ROOT, "pool": "fast"}
    assert pinned.pinned_devices == ["root"]
    assert effective_root_pool({"expanded_devices": pinned.expected_devices}) == "fast"
    assert rendered.devices == {}


def test_an_inherited_device_the_target_profile_renders_differently_is_pinned() -> None:
    data = {"type": "disk", "path": "/data", "source": "/srv/a"}
    source = _source(profile_devices={"root": SOURCE_ROOT, "eth0": SOURCE_NIC, "data": data})

    contract = _contract(source, target_profiles=_target(eth0=SOURCE_NIC, data={**data, "source": "/srv/b"}))

    assert contract.devices["data"] == data
    assert "data" in contract.pinned_devices


def test_a_profile_device_the_source_lacks_is_masked_with_none() -> None:
    contract = _contract(_source(), target_profiles=_target(eth0=SOURCE_NIC, gpu0={"type": "gpu"}))

    assert contract.devices["gpu0"] == {"type": "none"}
    assert "gpu0" in contract.pinned_devices
    assert "gpu0" not in contract.expected_devices


def test_a_copy_drops_explicit_mac_addresses_and_a_move_keeps_them() -> None:
    nic = {"type": "nic", "network": "lxdbr0", "hwaddr": "00:16:3e:11:22:33"}
    source = _source(local_devices={"eth1": nic})
    before = copy.deepcopy(source)

    copied = _contract(source, intent="copy")
    moved = _contract(source, intent="move")

    assert "hwaddr" not in copied.devices["eth1"]
    assert moved.devices["eth1"]["hwaddr"] == "00:16:3e:11:22:33"
    assert source == before


def test_config_the_target_profiles_would_change_is_pinned_locally() -> None:
    source = _source(
        local_config={"limits.cpu": "2"},
        profile_config={"limits.memory": "4GiB", "security.nesting": "true"},
    )
    target = _target(
        {"limits.memory": "8GiB", "security.nesting": "true", "boot.autostart": "true"}, eth0=SOURCE_NIC
    )

    contract = _contract(source, target_profiles=target)

    assert contract.config == {"limits.cpu": "2", "limits.memory": "4GiB"}
    assert contract.pinned_config == ["limits.memory"]
    assert contract.profile_added_config == ["boot.autostart"]
    assert contract.expected_config == {
        "limits.cpu": "2", "limits.memory": "4GiB", "security.nesting": "true", "boot.autostart": "true",
    }


def test_config_overrides_change_effective_values_and_local_ones_only_where_local() -> None:
    source = _source(local_config={"limits.cpu": "2"}, profile_config={"limits.memory": "4GiB"})
    target = _target({"limits.memory": "8GiB"}, eth0=SOURCE_NIC)

    pinned = _contract(source, target_profiles=target, overrides={"limits.cpu": "4", "limits.memory": "2GiB"})
    unpinned = _contract(source, target_profiles=target, overrides={"limits.memory": "8GiB"})

    assert pinned.config == {"limits.cpu": "4", "limits.memory": "2GiB"}
    assert unpinned.config == {"limits.cpu": "2"}
    assert unpinned.expected_config["limits.memory"] == "8GiB"


def test_the_contract_hash_is_stable_across_builds_key_order_and_the_job_marker() -> None:
    source = _source(local_config={"limits.cpu": "2", "limits.memory": "4GiB"})
    reordered = {
        key: dict(reversed(list(value.items()))) if isinstance(value, dict) else value
        for key, value in source.items()
    }
    contract = _contract(source)
    baseline = contract.contract_hash

    contract.config[MIGRATION_JOB_MARKER] = "job-1"

    assert contract.contract_hash == baseline
    assert _contract(source).contract_hash == baseline
    assert _contract(reordered).contract_hash == baseline


@pytest.mark.parametrize(
    "change",
    [
        {"root_pool": "slow"},
        {"network_mapping": {"lxdbr0": "ovn-a"}},
        {"profile_mapping": {"default": "default-b"}},
    ],
)
def test_the_contract_hash_changes_when_a_decision_changes(change: dict[str, Any]) -> None:
    target = {**DEFAULT_TARGET, "default-b": DEFAULT_TARGET["default"]}

    baseline = _contract(_source(), target_profiles=target).contract_hash

    changed = _contract(_source(), target_profiles=target, usable=("lxdbr0", "ovn-a"), **change)

    assert changed.contract_hash != baseline


def _readback(contract: TargetContract) -> dict[str, Any]:
    """The contract as a daemon reports it: generated MACs, volatile keys, marker, a none mask."""
    mac = "00:16:3e:bb:bb:02"
    devices = {
        name: {**device, "hwaddr": mac} if device.get("type") == "nic" else dict(device)
        for name, device in contract.expected_devices.items()
    }
    config = {**contract.expected_config, "volatile.eth0.hwaddr": mac, MIGRATION_JOB_MARKER: "job-1"}
    return {
        "profiles": list(contract.profiles),
        "expanded_config": config,
        "expanded_devices": {**devices, "gpu0": {"type": "none"}},
    }


def test_mismatches_ignore_generated_state_and_name_each_diverging_field() -> None:
    contract = _contract(_source())
    record = _readback(contract)

    assert mismatches(record, contract) == []
    assert target_matches(record, contract) is True

    record["profiles"] = ["other"]
    record["expanded_config"]["limits.cpu"] = "16"
    record["expanded_devices"]["eth0"]["network"] = "elsewhere"

    assert mismatches(record, contract) == ["profiles", "config:limits.cpu", "device:eth0"]
    assert mismatches(None, contract) == ["missing"]


def test_a_move_preserves_generated_macs_and_a_copy_does_not() -> None:
    explicit = {"type": "nic", "network": "lxdbr0", "hwaddr": "00:16:3e:cc:cc:03"}
    volatile = {f"volatile.eth{n}.hwaddr": f"00:16:3e:aa:aa:0{n}" for n in (0, 1, 2, 9)}
    source = _source(
        local_devices={"eth1": {"type": "nic", "network": "lxdbr0"}, "eth2": explicit}, volatile=volatile
    )
    contract = _contract(source)

    assert preserved_identity_config(source, contract, intent="move") == {
        "volatile.eth0.hwaddr": "00:16:3e:aa:aa:00",
        "volatile.eth1.hwaddr": "00:16:3e:aa:aa:01",
    }
    assert preserved_identity_config(source, contract, intent="copy") == {}


def test_the_definition_carries_preserved_identity_and_the_job_marker_without_aliasing() -> None:
    contract = _contract(_source())

    definition = contract.definition(
        instance_type="virtual-machine",
        architecture="x86_64",
        job_id="job-7",
        preserved={"volatile.eth0.hwaddr": "00:16:3e:aa:aa:01"},
    )
    definition["devices"]["root"]["pool"] = "mutated"

    assert definition["config"][MIGRATION_JOB_MARKER] == "job-7"
    assert definition["config"]["volatile.eth0.hwaddr"] == "00:16:3e:aa:aa:01"
    assert MIGRATION_JOB_MARKER not in contract.config
    assert contract.devices["root"]["pool"] == "fast"
