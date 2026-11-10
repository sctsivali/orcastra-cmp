"""What counts as an allocated GPU, and what does not.

Three shapes bind a GPU into an instance: a `type: gpu` device, the nvidia.runtime
family of config keys, and raw unix-char passthrough of an NVIDIA device node. Two shapes look
like allocations and are not: an LD_LIBRARY_PATH an image happens to carry, and a
/dev/dri/renderD* node that belongs to a display controller.
"""

from app.services.lxd.gpu import build_gpu_inventory

from tests.gpu_fixtures import (
    DISCRETE_RTX_CARD,
    JETSON_ORIN_NX_RESOURCES,
    FakeInstance,
    resources_with,
)

JETSON = JETSON_ORIN_NX_RESOURCES


def test_stopped_instances_hold_nothing() -> None:
    """Two stopped containers on a one-GPU host allocate nothing."""
    instances = [
        FakeInstance("ct-alphaco-qa-bcv-s-013a70bf", status="Stopped"),
        FakeInstance("ct-suka-hat-qa-gpu-h-531ac9f3", status="Stopped"),
    ]

    inventory = build_gpu_inventory(JETSON, instances)

    assert (inventory.gpu_total, inventory.gpu_used, inventory.gpu_available) == (1, 0, 1)


def test_a_profile_granted_gpu_is_counted() -> None:
    """An instance overriding nothing still holds the GPU its profile grants."""
    instance = FakeInstance(
        "ct-gpu", devices={}, expanded_devices={"gpu0": {"type": "gpu", "id": "1"}}
    )

    assert build_gpu_inventory(JETSON, [instance]).gpu_used == 1


def test_expanded_config_is_preferred_over_config() -> None:
    instance = FakeInstance("ct-gpu", config={}, expanded_config={"nvidia.runtime": "true"})

    assert build_gpu_inventory(JETSON, [instance]).gpu_used == 1


def test_an_instance_without_expanded_fields_still_works() -> None:
    """Older pylxd objects carry only devices/config."""
    instance = FakeInstance("ct-gpu", devices={"gpu0": {"type": "gpu"}})

    assert build_gpu_inventory(JETSON, [instance]).gpu_used == 1


def test_a_vaapi_transcode_does_not_consume_a_compute_gpu() -> None:
    """renderD128 on a Jetson is the display controller, not the GPU."""
    instance = FakeInstance(
        "ct-transcode",
        devices={"dri": {"type": "unix-char", "source": "/dev/dri/renderD128"}},
    )

    inventory = build_gpu_inventory(JETSON, [instance])

    assert inventory.gpu_used == 0, "a display node is not compute capacity"
    assert inventory.cards[0].in_use is True, "the card it names is still marked in use"
    assert inventory.cards[0].used_by == "ct-transcode"
    assert inventory.cards[1].in_use is False


def test_cuda_on_the_compute_render_node_does_consume_the_gpu() -> None:
    instance = FakeInstance(
        "ct-cuda", devices={"dri": {"type": "unix-char", "source": "/dev/dri/renderD129"}}
    )

    inventory = build_gpu_inventory(JETSON, [instance])

    assert inventory.gpu_used == 1
    assert inventory.cards[1].used_by == "ct-cuda"


def test_a_raw_nvidia_device_passthrough_is_counted() -> None:
    for node in ("/dev/nvidia0", "/dev/nvidiactl", "/dev/nvhost-gpu", "/dev/nvmap"):
        instance = FakeInstance("ct-gpu", devices={"d": {"type": "unix-char", "source": node}})
        assert build_gpu_inventory(JETSON, [instance]).gpu_used == 1, node


def test_a_library_path_alone_is_not_an_allocation() -> None:
    """An image can carry the variable with no device attached.

    Counting it would consume the host's only GPU on a container that never touches it,
    with no way for the operator to see why capacity is gone.
    """
    instance = FakeInstance(
        "ct-plain", config={"environment.LD_LIBRARY_PATH": "/usr/lib/aarch64-linux-gnu/tegra"}
    )

    assert build_gpu_inventory(JETSON, [instance]).gpu_used == 0


def test_two_instances_on_one_gpu_do_not_oversubscribe_the_count() -> None:
    instances = [
        FakeInstance("a", config={"nvidia.runtime": "true"}),
        FakeInstance("b", config={"nvidia.runtime": "true"}),
    ]

    inventory = build_gpu_inventory(JETSON, instances)

    assert inventory.gpu_used == 1, "capacity is the ceiling"
    assert inventory.gpu_available == 0


def test_gpu_available_is_never_negative() -> None:
    instances = [FakeInstance(str(i), config={"nvidia.runtime": "true"}) for i in range(5)]

    assert build_gpu_inventory(JETSON, instances).gpu_available == 0


def test_only_instances_on_this_member_are_counted() -> None:
    """Capacity comes from one member; usage must come from the same one."""
    instances = [
        FakeInstance("on-a", location="node-a", config={"nvidia.runtime": "true"}),
        FakeInstance("on-b", location="node-b", config={"nvidia.runtime": "true"}),
    ]

    inventory = build_gpu_inventory(JETSON, instances, member="node-a")

    assert inventory.gpu_used == 1
    assert inventory.cards[1].used_by in (None, "on-a")


def test_an_unclustered_instance_belongs_to_the_member_being_asked_about() -> None:
    """A standalone LXD host reports location "none", not null."""
    for location in ("none", "", None):
        instance = FakeInstance("ct", location=location, config={"nvidia.runtime": "true"})
        inventory = build_gpu_inventory(JETSON, [instance], member="panda")
        assert inventory.gpu_used == 1, f"location={location!r} was dropped"


def test_in_use_attaches_to_the_card_the_device_names_not_the_list_position() -> None:
    """LXD's device `id` is the DRM card id. Matching it against the list index would move the
    in-use flag onto whichever card sits at that position."""
    reversed_cards = list(reversed(JETSON["gpu"]["cards"]))
    resources = resources_with(reversed_cards, memory=JETSON["memory"], system=JETSON["system"])
    instance = FakeInstance("ct-gpu", devices={"gpu0": {"type": "gpu", "id": "1"}})

    cards = build_gpu_inventory(resources, [instance]).cards

    holder = [card for card in cards if card.in_use]
    assert len(holder) == 1
    assert holder[0].id == 1, "the card whose DRM id is 1, not cards[1]"
    assert holder[0].driver == "nv_platform"


def test_a_pci_device_matches_regardless_of_domain_padding() -> None:
    instance = FakeInstance("ct-gpu", devices={"gpu0": {"type": "gpu", "pci": "01:00.0"}})

    card = build_gpu_inventory(resources_with([DISCRETE_RTX_CARD]), [instance]).cards[0]

    assert card.pci_address == "0000:01:00.0"
    assert card.in_use is True, "01:00.0 and 0000:01:00.0 are the same device"


def test_a_vendorid_productid_pair_matches() -> None:
    instance = FakeInstance(
        "ct-gpu", devices={"gpu0": {"type": "gpu", "vendorid": "10de", "productid": "2684"}}
    )

    card = build_gpu_inventory(resources_with([DISCRETE_RTX_CARD]), [instance]).cards[0]

    assert card.in_use is True
    assert card.used_by == "ct-gpu"


def test_an_unqualified_gpu_device_holds_the_gpu() -> None:
    """`{"type": "gpu"}` with no selector means every GPU on the host."""
    instance = FakeInstance("ct-gpu", devices={"gpu0": {"type": "gpu"}})

    assert build_gpu_inventory(JETSON, [instance]).gpu_used == 1


def test_an_instance_enumeration_failure_does_not_zero_capacity() -> None:
    def exploding():
        raise RuntimeError("LXD went away mid-list")
        yield  # pragma: no cover

    inventory = build_gpu_inventory(JETSON, exploding())

    assert inventory.gpu_total == 1, "capacity is a property of the hardware, not of the scan"
    assert inventory.gpu_used == 0, "report no usage rather than a wrong usage"


def test_a_display_only_host_reports_cards_but_no_capacity() -> None:
    display_only = resources_with([JETSON["gpu"]["cards"][0]])

    inventory = build_gpu_inventory(display_only)

    assert inventory.gpu_total == 0
    assert inventory.gpu_cards_total == 1, "the UI must still be able to say what was detected"


def test_merging_members_keeps_the_totals_consistent() -> None:
    first = build_gpu_inventory(JETSON, [FakeInstance("a", config={"nvidia.runtime": "true"})])
    second = build_gpu_inventory(resources_with([DISCRETE_RTX_CARD]))

    merged = first.merge(second)

    assert (merged.gpu_total, merged.gpu_used, merged.gpu_available) == (2, 1, 1)
    assert merged.gpu_cards_total == 3
    assert merged.vendors == ["NVIDIA", "NVIDIA Corporation"]
