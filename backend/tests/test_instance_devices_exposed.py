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


def test_the_builder_populates_both_maps() -> None:
    """Guards the wiring, so removing either line fails here rather than in the browser."""
    import inspect

    from app.services.lxd import instance as instance_module

    src = inspect.getsource(instance_module._InstanceManagerBase._instance_to_vm_info) \
        if hasattr(instance_module, "_InstanceManagerBase") else inspect.getsource(instance_module)
    assert "devices=jsonable_lxd(" in src, "devices must be populated in _instance_to_vm_info"
    assert "expanded_devices=jsonable_lxd(" in src, (
        "expanded_devices must be populated in _instance_to_vm_info"
    )
