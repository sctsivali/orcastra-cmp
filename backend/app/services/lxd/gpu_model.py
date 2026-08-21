"""
GPU hardware model: how a raw LXD card is classified and identified.

LXD's /1.0/resources GPU block is the only input. It carries no telemetry (no busy
percentage, no VRAM usage), so nothing here derives a utilization figure. Counting and
allocation live in gpu.py, which builds on this module.
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

from app.models.schemas import (
    GPUCardInfo,
    GPUDRMInfo,
    GPUMdevProfile,
    GPUNvidiaInfo,
    GPUSRIOVInfo,
)

logger = logging.getLogger(__name__)

GPU_COMPUTE_DRIVERS = frozenset({
    "nvidia", "nv_platform", "nvgpu",   # NVIDIA discrete + Tegra iGPU compute node
    "amdgpu",                           # see _AMD_APU_PRODUCT_MARKERS
    "habanalabs", "hl",                 # Habana Gaudi
})

# Integrated graphics: an operator may pass these through (VAAPI, light OpenCL), but they
# are not sold as compute capacity.
GPU_INTEGRATED_DRIVERS = frozenset({"i915", "xe"})

# Display controllers: never capacity, never offered as a compute device.
GPU_DISPLAY_DRIVERS = frozenset({
    "drm", "tegra", "tegra-drm",                        # Tegra display node
    "ast", "mgag200", "cirrus", "bochs-drm",            # BMC / emulated server display
    "simple-framebuffer", "simpledrm", "efifb", "vesafb",
    "virtio-gpu", "vmwgfx", "hyperv_drm",               # virtual display inside a VM
})

# important: LXD reports no discrete/integrated bit for AMD. `amdgpu` drives both a Radeon
# RX 7900 and a Ryzen APU's iGPU, and neither vendor/product nor pci_address separates them.
# Every amdgpu card is classified `compute`: hiding a real discrete Radeon from every view is
# strictly worse than counting an APU an operator can see and ignore. These markers only set
# the unified-memory hint, they never change the class.
_AMD_APU_PRODUCT_MARKERS = (
    "renoir", "cezanne", "lucienne", "barcelo", "rembrandt", "phoenix",
    "raphael", "mendocino", "van gogh", "vega mobile", "radeon graphics",
)

# Intel ships discrete accelerators on the same drivers as its iGPUs, so the product string
# is the only separator LXD gives us.
_INTEL_DISCRETE_PRODUCT_MARKERS = ("arc ", "arc(", "data center gpu", "flex ", "max ", "ponte vecchio")

_DRIVER_VENDOR_LABELS = {
    "nvidia": "NVIDIA", "nv_platform": "NVIDIA", "nvgpu": "NVIDIA",
    "amdgpu": "AMD", "radeon": "AMD",
    "i915": "Intel", "xe": "Intel",
    "habanalabs": "Habana", "hl": "Habana",
}

# Device nodes that unambiguously mean "this instance holds a compute GPU".
@dataclass(frozen=True)
class GpuClassification:
    gpu_class: str        # "compute" | "integrated" | "display" | "unknown"
    is_compute: bool      # counts toward gpu_total
    is_integrated: bool   # on-die with the CPU, shares system RAM
    attachable: bool      # an operator may pass this device into an instance


def classify_gpu_card(card: Dict[str, Any]) -> GpuClassification:
    """Classify one raw LXD GPU card by driver.

    important: driver-first and driver-only. Two heuristics look tempting on a Jetson and
    both are wrong on the verified payload:
      1. "has a render node, therefore compute". The Tegra DISPLAY card exposes renderD128
         and the compute card exposes renderD129, so a render node proves nothing.
      2. "driver_version mentions tegra, therefore NVIDIA". The display card's
         driver_version is the KERNEL string, "5.15.148-tegra", so that rule would promote a
         display controller into sellable capacity.
    """
    driver = (card.get("driver") or "").strip().lower()
    product = (card.get("product") or "").strip().lower()
    has_nvidia_block = bool(card.get("nvidia"))

    if driver in GPU_DISPLAY_DRIVERS:
        return GpuClassification("display", False, False, False)

    if driver in GPU_COMPUTE_DRIVERS or has_nvidia_block:
        unified = (
            driver in ("nv_platform", "nvgpu")
            or (driver == "amdgpu" and any(m in product for m in _AMD_APU_PRODUCT_MARKERS))
        )
        return GpuClassification("compute", True, unified, True)

    if driver in GPU_INTEGRATED_DRIVERS:
        if any(m in product for m in _INTEL_DISCRETE_PRODUCT_MARKERS):
            return GpuClassification("compute", True, False, True)
        return GpuClassification("integrated", False, True, True)

    # fail-closed: an unrecognized driver is never sellable capacity and is never offered as
    # a compute device, but it stays in the card list with an honest label so an operator can
    # still see that the hardware exists.
    return GpuClassification("unknown", False, False, False)


def driver_vendor_label(driver: Optional[str]) -> Optional[str]:
    """Vendor implied by the kernel driver, for hardware that reports no vendor string."""
    return _DRIVER_VENDOR_LABELS.get((driver or "").strip().lower())


def _normalize_pci(addr: str) -> str:
    """LXD accepts `01:00.0` and `0000:01:00.0` for the same device."""
    text = (addr or "").strip().lower()
    if text and text.count(":") == 1:
        text = f"0000:{text}"
    return text


def card_match_keys(card: Dict[str, Any]) -> Set[str]:
    """Stable identifiers by which a device definition can name this card.

    important: list position is deliberately not a key. The previous parser matched a
    device's `id:` against `str(index)`, but LXD's `id:` is the DRM card id (drm.id). The two
    coincide only by luck, so a skipped or reordered card moved in_use/used_by onto the wrong
    row.
    """
    keys: Set[str] = set()
    drm = card.get("drm") or {}
    nvidia = card.get("nvidia") or {}

    if card.get("pci_address"):
        keys.add(f"pci:{_normalize_pci(card['pci_address'])}")
    if card.get("usb_address"):
        keys.add(f"usb:{str(card['usb_address']).strip().lower()}")
    if isinstance(nvidia, dict) and nvidia.get("uuid"):
        keys.add(f"uuid:{str(nvidia['uuid']).strip().lower()}")
    if isinstance(drm, dict):
        if drm.get("id") is not None:
            keys.add(f"drm:{drm['id']}")
        if drm.get("card_name"):
            keys.add(f"card:{str(drm['card_name']).strip().lower()}")
        if drm.get("render_name"):
            keys.add(f"render:{str(drm['render_name']).strip().lower()}")
    if card.get("vendor_id") and card.get("product_id"):
        keys.add(f"vp:{str(card['vendor_id']).lower()}:{str(card['product_id']).lower()}")
    return keys


def _device_match_keys(device: Dict[str, Any]) -> Set[str]:
    """The same identifier space, read off an LXD `type: gpu` device definition."""
    keys: Set[str] = set()
    if device.get("pci"):
        keys.add(f"pci:{_normalize_pci(str(device['pci']))}")
    if device.get("id") not in (None, ""):
        raw = str(device["id"]).strip().lower()
        keys.add(f"drm:{raw}")
        if raw.isdigit():
            keys.add(f"drm:{int(raw)}")
        keys.add(f"card:card{raw}" if raw.isdigit() else f"card:{raw}")
    if device.get("vendorid") and device.get("productid"):
        keys.add(f"vp:{str(device['vendorid']).lower()}:{str(device['productid']).lower()}")
    return keys


def parse_gpu_cards(
    gpu_cards_raw: Optional[List[Dict[str, Any]]],
    usage_map: Optional[Dict[str, str]] = None,
    system_memory_total: Optional[int] = None,
    system_product: Optional[str] = None,
    system_vendor: Optional[str] = None,
) -> List[GPUCardInfo]:
    """Turn the raw LXD card list into classified GPUCardInfo objects."""
    if not gpu_cards_raw:
        return []

    usage_map = usage_map or {}
    cards: List[GPUCardInfo] = []

    for index, card in enumerate(gpu_cards_raw):
        try:
            drm_info = None
            drm_raw = card.get("drm")
            if isinstance(drm_raw, dict):
                drm_info = GPUDRMInfo(
                    id=drm_raw.get("id"),
                    card_name=drm_raw.get("card_name"),
                    card_device=drm_raw.get("card_device"),
                    control_name=drm_raw.get("control_name"),
                    control_device=drm_raw.get("control_device"),
                    render_name=drm_raw.get("render_name"),
                    render_device=drm_raw.get("render_device"),
                )

            nvidia_info = None
            nvidia_raw = card.get("nvidia")
            if isinstance(nvidia_raw, dict):
                nvidia_info = GPUNvidiaInfo(
                    cuda_version=nvidia_raw.get("cuda_version"),
                    nvrm_version=nvidia_raw.get("nvrm_version"),
                    brand=nvidia_raw.get("brand"),
                    model=nvidia_raw.get("model"),
                    uuid=nvidia_raw.get("uuid"),
                    architecture=nvidia_raw.get("architecture"),
                    card_name=nvidia_raw.get("card_name"),
                    card_device=nvidia_raw.get("card_device"),
                )

            sriov_info = None
            sriov_raw = card.get("sriov")
            if isinstance(sriov_raw, dict):
                vfs = sriov_raw.get("vfs", [])
                sriov_info = GPUSRIOVInfo(
                    current_vfs=sriov_raw.get("current_vfs", 0),
                    maximum_vfs=sriov_raw.get("maximum_vfs", 0),
                    vfs_count=len(vfs) if isinstance(vfs, list) else 0,
                )

            mdev_profiles = []
            mdev_raw = card.get("mdev")
            if isinstance(mdev_raw, dict):
                for profile_name, profile_data in mdev_raw.items():
                    if isinstance(profile_data, dict):
                        mdev_profiles.append(GPUMdevProfile(
                            name=profile_data.get("name", profile_name),
                            api=profile_data.get("api"),
                            available=profile_data.get("available", 0),
                            description=profile_data.get("description"),
                            devices=profile_data.get("devices", []),
                        ))

            classification = classify_gpu_card(card)

            in_use = False
            used_by = None
            for key in card_match_keys(card):
                if key in usage_map:
                    in_use = True
                    used_by = usage_map[key]
                    break

            vendor = card.get("vendor")
            if not vendor:
                # Tegra and some platform GPUs report no vendor string at all. The kernel
                # driver names the vendor deterministically, and the driver is shown beside
                # it in the UI, so this is a derivation rather than an invention.
                vendor = driver_vendor_label(card.get("driver")) or (
                    system_vendor if classification.is_integrated else None
                )

            model_name = (nvidia_raw.get("model") if isinstance(nvidia_raw, dict) else None) or card.get("product")
            if not model_name and classification.is_integrated and system_product:
                # An integrated GPU has no card-level product string. The host system product
                # names it exactly when the machine is the GPU (a Jetson dev kit), which is
                # true only when the system product names the same vendor.
                if vendor and vendor.lower() in system_product.lower():
                    model_name = system_product

            memory_total = None
            memory_shared = False
            if classification.is_integrated and system_memory_total:
                memory_total = system_memory_total
                memory_shared = True

            cards.append(GPUCardInfo(
                index=index,
                id=drm_raw.get("id") if isinstance(drm_raw, dict) else None,
                driver=card.get("driver"),
                driver_version=card.get("driver_version"),
                pci_address=card.get("pci_address") or None,
                usb_address=card.get("usb_address") or None,
                vendor=vendor,
                vendor_id=card.get("vendor_id"),
                product=card.get("product"),
                product_id=card.get("product_id"),
                numa_node=card.get("numa_node"),
                in_use=in_use,
                used_by=used_by,
                gpu_class=classification.gpu_class,
                is_compute=classification.is_compute,
                is_integrated=classification.is_integrated,
                attachable=classification.attachable,
                model_name=model_name,
                memory_total=memory_total,
                memory_shared=memory_shared,
                drm=drm_info,
                nvidia=nvidia_info,
                sriov=sriov_info,
                mdev_profiles=mdev_profiles,
            ))

        except Exception as e:
            logger.warning(f"Failed to parse GPU card {index}: {e}")
            # A card that cannot be parsed stays visible with whatever LXD did give us. It is
            # never capacity and never attachable, so a parse failure can only under-report.
            drm_raw = card.get("drm") if isinstance(card.get("drm"), dict) else {}
            cards.append(GPUCardInfo(
                index=index,
                id=drm_raw.get("id"),
                driver=card.get("driver"),
                vendor=card.get("vendor") or driver_vendor_label(card.get("driver")),
                product=card.get("product"),
                gpu_class="unknown",
                is_compute=False,
                attachable=False,
            ))

    return cards

