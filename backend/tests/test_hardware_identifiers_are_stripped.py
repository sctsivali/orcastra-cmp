"""No asset identifier reaches a client, and no future field can quietly add one.

The Technologies page has no role gate and is reachable by Tenant, and the hardware profile
rides GET /nodes, so this payload is the most broadly visible in the product. The sibling
product solved the same problem in React, with a `hideSerials` prop on the component: the
serials, the system UUID and the MAC addresses all still crossed the wire and anyone with
devtools could read them, and the MAC was not even covered by the prop. Stripping happens in
the backend or it does not happen.

Two layers are asserted here. The value layer catches a stripper that stopped working. The
schema layer catches a model that gained a field, which is the failure that would otherwise
ship silently, because no fixture exists yet for a field nobody added on purpose.
"""

import json

from app.models.schemas import (
    ChassisInfo,
    CPUSocketInfo,
    CPUTopology,
    FirmwareInfo,
    GPUNvidiaInfo,
    HardwareProfile,
    MemoryTopology,
    MotherboardInfo,
    NetworkCardInfo,
    NetworkPortInfo,
    StorageDeviceInfo,
    SystemInfo,
)
from app.services.lxd.hardware import IDENTIFIER_FIELDS, build_hardware_profile
from app.services.lxd.resources import PUBLIC_ENVIRONMENT_KEYS, public_environment

from tests.hardware_fixtures import DUAL_SOCKET_HOST_RESOURCES, LEAK_SENTINELS

HARDWARE_MODELS = [
    FirmwareInfo,
    ChassisInfo,
    MotherboardInfo,
    SystemInfo,
    CPUSocketInfo,
    CPUTopology,
    MemoryTopology,
    NetworkPortInfo,
    NetworkCardInfo,
    StorageDeviceInfo,
    HardwareProfile,
]


def test_no_identifier_survives_a_payload_that_carries_every_one_of_them():
    """The fixture plants thirteen sentinels. None may appear in the serialized profile.

    Asserted on values rather than key names: renaming a field must not be able to defeat it.
    """
    profile = build_hardware_profile(DUAL_SOCKET_HOST_RESOURCES)
    blob = json.dumps(profile.model_dump())

    leaked = [sentinel for sentinel in LEAK_SENTINELS if sentinel in blob]
    assert leaked == [], f"identifiers reached the serialized profile: {leaked}"


def test_the_facts_worth_showing_did_survive():
    """The counterpart to the test above: a stripper that emits nothing also passes it."""
    profile = build_hardware_profile(DUAL_SOCKET_HOST_RESOURCES)

    assert profile.system is not None
    assert profile.system.vendor == "Dell Inc."
    assert profile.system.product == "PowerEdge R650"
    assert profile.system.firmware.version == "2.10.2"
    assert profile.system.motherboard.product == "0X2R63"
    assert profile.networks[0].product == "I350 Gigabit Network Connection"
    assert profile.networks[0].ports[0].link_speed == 1000
    assert profile.storage_devices[0].model == "SAMSUNG MZQL21T9HCJR-00A07"


def test_no_hardware_model_declares_an_identifier_field():
    """The ratchet.

    This fails the day someone adds `serial: Optional[str]` to StorageDeviceInfo, which is
    before any fixture exists to catch it and therefore before the value assertion above can.
    """
    offenders = []
    for model in HARDWARE_MODELS:
        for field_name in model.model_fields:
            if field_name in IDENTIFIER_FIELDS:
                offenders.append(f"{model.__name__}.{field_name}")
    assert offenders == [], f"identifier fields declared on a serialized model: {offenders}"


def test_every_hardware_model_forbids_extra_keys():
    """Without this the allowlist is a suggestion.

    extra="forbid" is what makes a new LXD identifier unconstructible rather than merely
    unmentioned, so a normalizer that grows a passthrough cannot leak by accident.
    """
    permissive = [m.__name__ for m in HARDWARE_MODELS if m.model_config.get("extra") != "forbid"]
    assert permissive == [], f"models accepting unknown keys: {permissive}"


def test_the_pci_and_usb_sections_are_not_carried_at_all():
    """Both are pure device inventories full of addresses and serials, and nothing renders them."""
    blob = json.dumps(build_hardware_profile(DUAL_SOCKET_HOST_RESOURCES).model_dump())
    assert '"pci"' not in blob
    assert '"usb"' not in blob


def test_a_partition_table_is_reduced_to_a_count():
    """Every partition entry carries its own device name and id, and only the count is read."""
    profile = build_hardware_profile(DUAL_SOCKET_HOST_RESOURCES)
    assert profile.storage_devices[0].partitions == 3
    assert "LEAK-PARTITION-ID" not in json.dumps(profile.model_dump())


def test_a_gpu_uuid_is_readable_internally_and_absent_from_the_response():
    """A GPU UUID is a per-device serial, and this model rides the same GET /nodes body.

    It stays on the object because gpu_model.py matches cards by it. It leaves the model on
    the way out, because a Tenant has no use for it.
    """
    nvidia = GPUNvidiaInfo(uuid="GPU-4b2a-LEAK", model="A100", cuda_version="12.2")

    assert nvidia.uuid == "GPU-4b2a-LEAK", "the matcher still needs to read it"
    assert "uuid" not in nvidia.model_dump(), "it must not be serialized"
    assert "GPU-4b2a-LEAK" not in json.dumps(nvidia.model_dump())


def test_the_environment_block_drops_the_cluster_certificate():
    """NodeStatus.environment is typed Dict[str, Any], so response_model filters nothing.

    The raw LXD block carries the cluster's certificate PEM, its fingerprint, every listen
    address and the server PID, and it rides the same ungated GET /nodes.
    """
    raw = {
        "os_name": "Ubuntu",
        "kernel_version": "6.8.0-136-generic",
        "certificate": "-----BEGIN CERTIFICATE-----LEAKPEM",
        "certificate_fingerprint": "LEAKFINGERPRINT",
        "addresses": ["10.0.0.1:8443"],
        "server_pid": 2759958,
    }
    public = public_environment(raw)

    assert set(public) == {"os_name", "kernel_version"}
    blob = json.dumps(public)
    for sentinel in ("LEAKPEM", "LEAKFINGERPRINT", "10.0.0.1:8443", "2759958"):
        assert sentinel not in blob


def test_the_environment_allowlist_names_no_identifier():
    """Allowlist, so the failure mode is omission rather than disclosure. Keep it that way."""
    offenders = sorted(PUBLIC_ENVIRONMENT_KEYS & IDENTIFIER_FIELDS)
    assert offenders == [], f"identifier keys allowed onto the wire: {offenders}"


def test_an_empty_environment_is_passed_through_untouched():
    """A node that answered with nothing must stay distinguishable from one that answered."""
    assert public_environment(None) is None
    assert public_environment({}) == {}
