"""The root disk size an operator chooses at create time must be readable back.

The create form accepts a size, sends it as `devices.root.size`, and LXD stores it on the
instance's own device config. Nothing returned it: the instance detail response carried no
devices field, and the storage volume for a VM root reports only `volatile.uuid`. An operator
could pick a size and then never see it again, and a test had no surface to assert it against.
"""

from app.models.schemas import VMInfo
from app.services.lxd.utils import jsonable_lxd


class _AttributeDict:
    """pylxd hands device maps back as AttributeDict, which is NOT a dict subclass."""

    def __init__(self, **kw) -> None:
        self.__dict__.update(kw)


def test_vminfo_carries_devices_and_expanded_devices() -> None:
    fields = VMInfo.model_fields
    assert "devices" in fields, "the instance's own device overrides must be exposed"
    assert "expanded_devices" in fields, (
        "profile-expanded devices must be exposed too: an instance that overrides nothing still "
        "has an effective root disk, and that is what an operator needs to see"
    )


def test_a_root_disk_size_survives_the_pylxd_conversion() -> None:
    devices = {"root": _AttributeDict(type="disk", path="/", pool="default", size="20GiB")}

    converted = jsonable_lxd(devices)

    assert converted == {
        "root": {"type": "disk", "path": "/", "pool": "default", "size": "20GiB"}
    }, f"the size must survive as a plain string, got {converted!r}"


def test_an_instance_with_no_overrides_reports_an_empty_map_not_null() -> None:
    """`{}` says "this instance overrides nothing"; `null` says "we did not look"."""
    assert jsonable_lxd({}) == {}
    assert jsonable_lxd(None) is None


def test_every_build_path_carries_devices() -> None:
    """Three separate paths build an instance response, and the first fix only covered one.

    `_instance_to_vm_info` serves start/stop and the pylxd-model path; a second builder inside
    `list_all_vms` constructs VMInfo straight from the raw LXD payload; and the detail endpoint
    goes through `InstanceWrapper`, which copies a fixed set of fields and silently dropped
    anything not on that list. Miss one and the field reads as null or {} on exactly the surface
    an operator uses, which is how the first attempt shipped looking correct and returning
    nothing.
    """
    import inspect

    from app.services.lxd import instance as instance_module
    from app.services.lxd import utils as utils_module

    inst_src = inspect.getsource(instance_module)
    assert "devices=jsonable_lxd(" in inst_src, "the pylxd-model builder must populate devices"
    assert "expanded_devices=jsonable_lxd(" in inst_src, (
        "the pylxd-model builder must populate expanded_devices"
    )
    # The list builder constructs VMInfo from the raw all-projects payload and must carry both.
    row = {
        "name": "web", "status": "Running", "status_code": 103, "project": "p",
        "devices": {"root": {"type": "disk"}}, "expanded_devices": {"root": {"type": "disk"}, "eth0": {"type": "nic"}},
    }
    built = instance_module._vm_info_from_row(row, "nuc2", "")
    assert built.devices == row["devices"] and built.expanded_devices == row["expanded_devices"]

    wrapper_src = inspect.getsource(utils_module.InstanceWrapper.__init__)
    assert "self.devices" in wrapper_src, (
        "InstanceWrapper copies a fixed field list; devices must be on it or the detail "
        "endpoint returns an empty map"
    )
    assert "self.expanded_devices" in wrapper_src, (
        "InstanceWrapper must carry expanded_devices as well"
    )
