"""A GPU card is classified by driver, and only by driver.

The Jetson Orin NX is the hardware that forces this. It enumerates two DRM cards for one
physical GPU, so counting cards double-counts capacity, and the two heuristics that look
obvious both promote the display controller into sellable capacity: the display card owns a
render node (renderD128), and its driver_version is the kernel string "5.15.148-tegra".
"""

from app.services.lxd.gpu import build_gpu_inventory, classify_gpu_card, parse_gpu_cards

from tests.gpu_fixtures import (
    AMD_APU_CARD,
    AMD_MI210_CARD,
    ASPEED_BMC_CARD,
    DISCRETE_RTX_CARD,
    HABANA_CARD,
    INTEL_ARC_CARD,
    INTEL_UHD_CARD,
    JETSON_COMPUTE_CARD,
    JETSON_DISPLAY_CARD,
    JETSON_ORIN_NX_RESOURCES,
    MALFORMED_CARD,
    SRIOV_MDEV_CARD,
    UNKNOWN_DRIVER_CARD,
    resources_with,
)


def test_the_jetson_reports_one_compute_gpu_of_two_cards() -> None:
    inventory = build_gpu_inventory(JETSON_ORIN_NX_RESOURCES)

    assert inventory.gpu_total == 1, "one physical GPU, not two DRM cards"
    assert inventory.gpu_cards_total == 2, "both cards stay visible"
    assert inventory.gpu_display_total == 1
    assert [card.gpu_class for card in inventory.cards] == ["display", "compute"]


def test_the_tegra_display_card_is_not_promoted_by_its_render_node() -> None:
    assert JETSON_DISPLAY_CARD["drm"]["render_name"] == "renderD128", "fixture guard"

    classification = classify_gpu_card(JETSON_DISPLAY_CARD)

    assert classification.gpu_class == "display"
    assert classification.is_compute is False
    assert classification.attachable is False


def test_a_kernel_version_in_driver_version_does_not_make_a_card_nvidia() -> None:
    assert "tegra" in JETSON_DISPLAY_CARD["driver_version"], "fixture guard"

    assert classify_gpu_card(JETSON_DISPLAY_CARD).is_compute is False


def test_the_jetson_igpu_reports_unified_memory_and_the_system_product_name() -> None:
    cards = build_gpu_inventory(JETSON_ORIN_NX_RESOURCES).cards
    compute = cards[1]

    assert compute.memory_total == 16416313344, "the iGPU shares system RAM"
    assert compute.memory_shared is True
    assert compute.model_name == JETSON_ORIN_NX_RESOURCES["system"]["product"]
    assert compute.vendor == "NVIDIA", "derived from the driver; LXD reports no vendor here"


def test_a_discrete_card_keeps_its_own_model_and_claims_no_shared_memory() -> None:
    card = build_gpu_inventory(resources_with([DISCRETE_RTX_CARD], memory={"total": 999})).cards[0]

    assert card.model_name == "NVIDIA GeForce RTX 4090"
    assert card.memory_shared is False
    assert card.memory_total is None, "LXD does not report discrete VRAM; do not borrow system RAM"


def test_an_amd_card_counts_as_compute() -> None:
    """LXD cannot tell an Instinct from an APU, so amdgpu is capacity and the APU is flagged.

    Hiding a real discrete Radeon from every view is worse than counting an APU an operator
    can see and ignore.
    """
    assert classify_gpu_card(AMD_MI210_CARD).gpu_class == "compute"
    assert classify_gpu_card(AMD_MI210_CARD).is_integrated is False


def test_an_amd_apu_is_still_compute_but_flagged_as_unified() -> None:
    classification = classify_gpu_card(AMD_APU_CARD)

    assert classification.gpu_class == "compute"
    assert classification.is_integrated is True


def test_an_intel_igpu_is_attachable_but_is_not_capacity() -> None:
    inventory = build_gpu_inventory(resources_with([INTEL_UHD_CARD]))

    assert inventory.gpu_total == 0, "an iGPU on every office NUC is not sellable GPU capacity"
    assert inventory.gpu_integrated_total == 1
    assert inventory.cards[0].attachable is True, "it can still be passed through for VAAPI"


def test_a_discrete_intel_accelerator_is_capacity() -> None:
    inventory = build_gpu_inventory(resources_with([INTEL_ARC_CARD]))

    assert inventory.gpu_total == 1, "Arc and Data Center GPU ship on the same driver as the iGPU"
    assert inventory.cards[0].gpu_class == "compute"


def test_habana_counts_as_compute() -> None:
    assert classify_gpu_card(HABANA_CARD).is_compute is True
    assert classify_gpu_card({"driver": "hl"}).is_compute is True


def test_a_bmc_display_controller_is_display_not_unknown() -> None:
    """Every rack server has one. Left unclassified it reads as mystery hardware."""
    assert classify_gpu_card(ASPEED_BMC_CARD).gpu_class == "display"


def test_a_virtio_gpu_reported_by_its_pci_transport_is_display() -> None:
    """A nested LXD VM reports driver "virtio-pci" and product "Virtio 1.0 GPU" (Console Lab)."""
    card = {"driver": "virtio-pci", "vendor": "Red Hat, Inc.", "product": "Virtio 1.0 GPU"}
    classified = classify_gpu_card(card)
    assert (classified.gpu_class, classified.is_compute, classified.attachable) == ("display", False, False)
    # Only the GPU: another virtio device behind the same transport stays unknown.
    assert classify_gpu_card({"driver": "virtio-pci", "product": "Virtio 1.0 network device"}).gpu_class == "unknown"


def test_an_unknown_driver_fails_closed_but_stays_visible() -> None:
    inventory = build_gpu_inventory(resources_with([UNKNOWN_DRIVER_CARD]))

    assert inventory.gpu_total == 0, "never sell capacity we cannot identify"
    assert inventory.gpu_unknown_total == 1
    assert inventory.gpu_cards_total == 1
    assert inventory.cards[0].driver == "wibble", "the operator still sees the hardware"
    assert inventory.cards[0].attachable is False


def test_a_card_that_cannot_be_parsed_still_appears_and_is_not_capacity() -> None:
    inventory = build_gpu_inventory(resources_with([MALFORMED_CARD]))

    assert inventory.gpu_cards_total == 1
    assert len(inventory.cards) == 1, "a parse failure must not delete the row"
    assert inventory.cards[0].is_compute is False
    assert inventory.cards[0].id == 7, "keep whatever identity LXD did give us"


def test_sriov_and_mdev_survive_the_parse() -> None:
    card = build_gpu_inventory(resources_with([SRIOV_MDEV_CARD])).cards[0]

    assert card.sriov is not None
    assert (card.sriov.current_vfs, card.sriov.maximum_vfs, card.sriov.vfs_count) == (2, 16, 2)
    assert [profile.name for profile in card.mdev_profiles] == ["GRID A100-10C"]
    assert card.mdev_profiles[0].available == 8


def test_the_vendor_list_survives_hardware_that_reports_no_vendor() -> None:
    inventory = build_gpu_inventory(JETSON_ORIN_NX_RESOURCES)

    assert inventory.vendors == ["NVIDIA"]
    assert inventory.models == [JETSON_ORIN_NX_RESOURCES["system"]["product"]]


def test_only_compute_cards_contribute_vendors_and_models() -> None:
    inventory = build_gpu_inventory(resources_with([ASPEED_BMC_CARD, DISCRETE_RTX_CARD]))

    assert inventory.vendors == ["NVIDIA Corporation"], "a BMC display chip is not a GPU vendor"


def test_a_host_with_no_gpu_block_reports_zeros_without_raising() -> None:
    inventory = build_gpu_inventory({})

    assert (inventory.gpu_total, inventory.gpu_cards_total, inventory.gpu_used) == (0, 0, 0)
    assert inventory.cards == []


def test_parse_returns_nothing_for_an_empty_card_list() -> None:
    assert parse_gpu_cards([]) == []
    assert parse_gpu_cards(None) == []
