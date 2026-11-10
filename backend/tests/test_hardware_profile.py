"""Absence is reported as absence, and a count is counted rather than inferred.

Every section of GET /1.0/resources is optional and varies by LXD version and host type. Where
the node said nothing the profile carries nothing: no plausible default and no row of blanks.

One NodeStatus row can be a whole MicroCloud, so the fold keeps a singular fact only when every
member agrees on it.
"""

from app.models.schemas import HardwareProfile
from app.services.lxd.hardware import (
    build_hardware_profile,
    cpu_model_from_sockets,
    merge_hardware_profiles,
    storage_types_from_disks,
)

from tests.gpu_fixtures import JETSON_ORIN_NX_RESOURCES
from tests.hardware_fixtures import (
    DUAL_SOCKET_HOST_RESOURCES,
    HOMOGENEOUS_MICROCLOUD_MEMBERS,
    MIXED_MICROCLOUD_MEMBERS,
    NESTED_CONTAINER_RESOURCES,
)


def test_the_cpu_walk_counts_cores_and_threads_from_the_topology():
    """Two sockets of twelve cores, two threads each, pinned to two NUMA nodes."""
    cpu = build_hardware_profile(DUAL_SOCKET_HOST_RESOURCES).cpu

    assert cpu.total == 48
    assert cpu.cores == 24
    assert cpu.threads == 48
    assert cpu.numa_nodes == 2
    assert len(cpu.sockets) == 2
    assert all(socket.cores == 12 and socket.threads == 24 for socket in cpu.sockets)


def test_cpu_total_is_not_mistaken_for_a_core_count():
    """The Jetson reports a total and no socket array.

    Copying the total into `cores` would claim a topology the node never described, so only the
    eight logical CPUs are reported.
    """
    cpu = build_hardware_profile(JETSON_ORIN_NX_RESOURCES).cpu

    assert cpu.total == 8
    assert cpu.cores == 0
    assert cpu.threads == 0
    assert cpu.sockets == []


def test_a_socket_vendor_is_normalized_but_a_model_name_is_not():
    """The vendor register value (GenuineIntel) is normalized; the model name is kept."""
    socket = build_hardware_profile(DUAL_SOCKET_HOST_RESOURCES).cpu.sockets[0]

    assert socket.vendor == "Intel"
    assert socket.name == "Intel(R) Xeon(R) Gold 6248 CPU @ 2.50GHz"


def test_a_payload_with_no_system_section_still_reports_cpu_and_memory():
    """A nested container reads no DMI. That must not cost it the rest of the profile."""
    profile = build_hardware_profile(NESTED_CONTAINER_RESOURCES)

    assert profile.system is None
    assert profile.detected is True
    assert profile.cpu.cores == 4
    assert profile.memory.total == 8589934592


def test_an_absent_system_section_is_none_rather_than_a_row_of_blanks():
    """SystemInfo() with eight Nones would render as eight empty rows."""
    assert build_hardware_profile(NESTED_CONTAINER_RESOURCES).system is None
    assert build_hardware_profile({"system": {}}).system is None


def test_threads_with_no_numa_node_report_zero_nodes():
    assert build_hardware_profile(NESTED_CONTAINER_RESOURCES).cpu.numa_nodes == 0


def test_architecture_is_never_defaulted():
    """routers/regions.py defaults a missing architecture to x86_64; the profile reports none."""
    payload = {"cpu": {"total": 4}, "memory": {"total": 1}}
    assert build_hardware_profile(payload).cpu.architectures == []


def test_free_memory_is_never_negative():
    """Total and used are sampled at different moments, so used can exceed total."""
    payload = {"memory": {"total": 100, "used": 140}}
    assert build_hardware_profile(payload).memory.free == 0


def test_an_empty_body_reports_not_detected():
    """The UI reads this flag to show nothing instead of zeros."""
    assert build_hardware_profile(None).detected is False
    assert build_hardware_profile({}).detected is False


def test_a_malformed_section_does_not_cost_the_caller_the_whole_profile():
    """A malformed section yields an undetected profile instead of an error."""
    profile = build_hardware_profile({"cpu": "not a dict", "memory": {"total": 1}})
    assert isinstance(profile, HardwareProfile)
    assert profile.detected is False


def test_a_spinning_disk_is_not_labelled_a_solid_state_one():
    """LXD emits no `rotational` key; `rpm` decides between HDD and SSD."""
    assert storage_types_from_disks({"disks": [{"type": "sata", "rpm": 7200}]}) == ["HDD"]
    assert storage_types_from_disks({"disks": [{"type": "sata", "rpm": 0}]}) == ["SATA SSD"]


def test_a_disk_that_does_not_report_rotation_is_not_guessed_either_way():
    assert storage_types_from_disks({"disks": [{"type": "sata"}]}) == ["SATA"]


def test_the_cpu_model_is_dropped_when_sockets_disagree():
    """A mixed-socket host has no single CPU model, and the first one is not the machine's."""
    agreeing = {"sockets": [{"name": "Xeon Gold 6248"}, {"name": "Xeon Gold 6248"}]}
    disagreeing = {"sockets": [{"name": "Xeon Gold 6248"}, {"name": "Xeon Silver 4210"}]}

    assert cpu_model_from_sockets(agreeing) == "Xeon Gold 6248"
    assert cpu_model_from_sockets(disagreeing) is None


def test_a_mixed_cluster_reports_no_single_machine():
    """Three members, two Dells and a Supermicro. The cluster is not a PowerEdge."""
    profiles = [build_hardware_profile(m, member=f"m{i}") for i, m in enumerate(MIXED_MICROCLOUD_MEMBERS)]
    folded = merge_hardware_profiles(profiles)

    assert folded.scope == "cluster"
    assert folded.member_count == 3
    assert folded.system is not None
    assert folded.system.product is None, "members disagree on the model"
    assert folded.system.vendor is None, "members disagree on the vendor"
    assert folded.system.motherboard is None, "a board is a per-unit fact"
    assert folded.system.firmware is None, "so is a BIOS version"


def test_a_homogeneous_cluster_keeps_the_fact_every_member_agrees_on():
    """A fact every member agrees on survives the fold."""
    profiles = [build_hardware_profile(m, member=f"m{i}") for i, m in enumerate(HOMOGENEOUS_MICROCLOUD_MEMBERS)]
    folded = merge_hardware_profiles(profiles)

    assert folded.system.product == "PowerEdge R650"
    assert folded.system.vendor == "Dell Inc."


def test_a_mixed_architecture_cluster_reports_both():
    """Impossible to express as a scalar, which is why architectures is a list."""
    profiles = [build_hardware_profile(m, member=f"m{i}") for i, m in enumerate(MIXED_MICROCLOUD_MEMBERS)]
    assert merge_hardware_profiles(profiles).cpu.architectures == ["aarch64", "x86_64"]


def test_cluster_capacity_is_the_sum_of_its_members():
    profiles = [build_hardware_profile(m, member=f"m{i}") for i, m in enumerate(HOMOGENEOUS_MICROCLOUD_MEMBERS)]
    folded = merge_hardware_profiles(profiles)

    assert folded.cpu.cores == 72
    assert folded.cpu.total == 144
    assert folded.memory.total == 274877906944 * 3


def test_every_device_row_names_the_member_it_came_from():
    """Otherwise twenty-four disks read as one machine's."""
    profiles = [build_hardware_profile(m, member=f"m{i}") for i, m in enumerate(HOMOGENEOUS_MICROCLOUD_MEMBERS)]
    folded = merge_hardware_profiles(profiles)

    members = {device.member for device in folded.storage_devices}
    assert members == {"m0", "m1", "m2"}


def test_identical_sockets_are_deduped_and_lose_a_meaningless_index():
    """Socket 0 of member 1 and socket 0 of member 2 are not the same socket."""
    profiles = [build_hardware_profile(m, member=f"m{i}") for i, m in enumerate(HOMOGENEOUS_MICROCLOUD_MEMBERS)]
    folded = merge_hardware_profiles(profiles)

    assert len(folded.cpu.sockets) == 1, "three identical members describe one CPU type"
    assert folded.cpu.sockets[0].socket is None


def test_a_single_member_fold_does_not_claim_cluster_scope():
    """The aggregation falls back to a single host when members cannot be read."""
    one = build_hardware_profile(DUAL_SOCKET_HOST_RESOURCES)
    folded = merge_hardware_profiles([one])

    assert folded.scope == "node"
    assert folded.member_count == 1


def test_folding_nothing_reports_not_detected():
    assert merge_hardware_profiles([]).detected is False
    assert merge_hardware_profiles([HardwareProfile(detected=False)]).detected is False
def test_a_dmi_placeholder_is_treated_as_absent():
    """A barebones board reports "Default string" as its vendor and product.

    Shown as the make and model it reads as a broken page, so a DMI placeholder counts as absent.
    """
    payload = {
        "system": {
            "vendor": "Default string",
            "product": "Default string",
            "family": "To Be Filled By O.E.M.",
            "firmware": {"vendor": "American Megatrends International, LLC.", "version": "GF1744"},
        },
        "cpu": {"total": 8},
    }
    system = build_hardware_profile(payload).system

    assert system is not None, "the firmware block is real, so the section stays"
    assert system.vendor is None
    assert system.product is None
    assert system.family is None
    assert system.firmware.vendor == "American Megatrends International, LLC."


def test_a_system_block_of_nothing_but_placeholders_disappears_entirely():
    payload = {"system": {"vendor": "Default string", "product": "To be filled by O.E.M.",
                          "chassis": {"vendor": "Default string", "type": "Default string"}}}
    assert build_hardware_profile(payload).system is None


def test_a_placeholder_only_sub_block_is_dropped_without_taking_the_section():
    payload = {"system": {"vendor": "Dell Inc.", "chassis": {"vendor": "Default string"}}}
    system = build_hardware_profile(payload).system

    assert system.vendor == "Dell Inc."
    assert system.chassis is None, "three empty rows under a heading is not a chassis section"
