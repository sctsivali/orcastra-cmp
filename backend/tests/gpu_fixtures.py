"""Hardware payloads for the GPU tests.

JETSON_ORIN_NX_RESOURCES is the verbatim /1.0/resources body from the registered host
`pandora` (standalone LXD 5.21.6). It is the reason the GPU model exists: the Orin NX
enumerates two DRM cards for one physical GPU, reports no vendor, no product and no PCI
address on either, and carries no `nvidia` block at all.
"""

from typing import Any, Dict, List, Optional

JETSON_DISPLAY_CARD: Dict[str, Any] = {
    "driver": "drm",
    "driver_version": "5.15.148-tegra",
    "drm": {
        "id": 0,
        "card_name": "card0",
        "card_device": "226:0",
        "control_name": "controlD64",
        "control_device": "226:0",
        "render_name": "renderD128",
        "render_device": "226:128",
    },
    "numa_node": 0,
}

JETSON_COMPUTE_CARD: Dict[str, Any] = {
    "driver": "nv_platform",
    "driver_version": "540.4.0",
    "drm": {
        "id": 1,
        "card_name": "card1",
        "card_device": "226:1",
        "control_name": "controlD65",
        "control_device": "226:1",
        "render_name": "renderD129",
        "render_device": "226:129",
    },
    "numa_node": 0,
}

JETSON_ORIN_NX_RESOURCES: Dict[str, Any] = {
    "gpu": {"cards": [JETSON_DISPLAY_CARD, JETSON_COMPUTE_CARD], "total": 2},
    "memory": {"total": 16416313344},
    "system": {
        "product": "NVIDIA Jetson Orin NX Engineering Reference Developer Kit Super",
        "vendor": "NVIDIA",
    },
    "cpu": {"architecture": "aarch64", "total": 8},
}

DISCRETE_RTX_CARD: Dict[str, Any] = {
    "driver": "nvidia",
    "driver_version": "550.54.14",
    "pci_address": "0000:01:00.0",
    "vendor": "NVIDIA Corporation",
    "vendor_id": "10de",
    "product": "AD102 [GeForce RTX 4090]",
    "product_id": "2684",
    "numa_node": 0,
    "drm": {"id": 0, "card_name": "card0", "render_name": "renderD128"},
    "nvidia": {
        "cuda_version": "12.4",
        "nvrm_version": "550.54.14",
        "brand": "GeForce",
        "model": "NVIDIA GeForce RTX 4090",
        "uuid": "GPU-1234abcd-5678-90ef-1234-567890abcdef",
        "architecture": "8.9",
    },
}

AMD_MI210_CARD: Dict[str, Any] = {
    "driver": "amdgpu",
    "driver_version": "6.7.0",
    "pci_address": "0000:63:00.0",
    "vendor": "Advanced Micro Devices, Inc.",
    "vendor_id": "1002",
    "product": "Aldebaran [Instinct MI210]",
    "product_id": "740f",
}

AMD_APU_CARD: Dict[str, Any] = {
    "driver": "amdgpu",
    "driver_version": "6.7.0",
    "pci_address": "0000:07:00.0",
    "vendor": "Advanced Micro Devices, Inc.",
    "product": "Raphael",
}

INTEL_UHD_CARD: Dict[str, Any] = {
    "driver": "i915",
    "driver_version": "6.5.0",
    "pci_address": "0000:00:02.0",
    "vendor": "Intel Corporation",
    "vendor_id": "8086",
    "product": "AlderLake-P GT2 [Iris Xe Graphics]",
    "drm": {"id": 0, "card_name": "card0", "render_name": "renderD128"},
}

INTEL_ARC_CARD: Dict[str, Any] = {
    "driver": "xe",
    "driver_version": "6.8.0",
    "pci_address": "0000:03:00.0",
    "vendor": "Intel Corporation",
    "product": "DG2 [Arc A770]",
}

HABANA_CARD: Dict[str, Any] = {
    "driver": "habanalabs",
    "driver_version": "1.15.0",
    "pci_address": "0000:19:00.0",
    "vendor": "Habana Labs Ltd.",
    "product": "Gaudi2",
}

ASPEED_BMC_CARD: Dict[str, Any] = {
    "driver": "ast",
    "driver_version": "6.5.0",
    "pci_address": "0000:07:00.0",
    "vendor": "ASPEED Technology, Inc.",
    "product": "ASPEED Graphics Family",
}

UNKNOWN_DRIVER_CARD: Dict[str, Any] = {
    "driver": "wibble",
    "driver_version": "0.1",
    "pci_address": "0000:09:00.0",
    "vendor": "Wibble Corp",
    "product": "Wibble Accelerator 9000",
}

SRIOV_MDEV_CARD: Dict[str, Any] = {
    "driver": "nvidia",
    "driver_version": "550.54.14",
    "pci_address": "0000:af:00.0",
    "vendor": "NVIDIA Corporation",
    "product": "GA100GL [A100 80GB PCIe]",
    "nvidia": {"model": "NVIDIA A100 80GB PCIe", "uuid": "GPU-aaaa"},
    "sriov": {"current_vfs": 2, "maximum_vfs": 16, "vfs": [{"id": 0}, {"id": 1}]},
    "mdev": {
        "nvidia-471": {
            "name": "GRID A100-10C",
            "api": "vfio-pci",
            "available": 8,
            "description": "num_heads=1, frl_config=60",
            "devices": ["/sys/bus/pci/devices/0000:af:00.0"],
        }
    },
}

# `current_vfs` is a string where the schema wants an int, which trips the same per-card
# except branch a malformed LXD payload would.
MALFORMED_CARD: Dict[str, Any] = {
    "driver": "nvidia",
    "vendor": "NVIDIA Corporation",
    "product": "Broken Card",
    "drm": {"id": 7, "card_name": "card7"},
    "sriov": {"current_vfs": "two", "maximum_vfs": 16, "vfs": []},
}


def resources_with(cards: List[Dict[str, Any]], **extra: Any) -> Dict[str, Any]:
    """A minimal /1.0/resources body carrying the given cards."""
    body: Dict[str, Any] = {"gpu": {"cards": cards, "total": len(cards)}}
    body.update(extra)
    return body


class FakeInstance:
    """The subset of a pylxd instance the GPU allocation scan reads."""

    def __init__(
        self,
        name: str,
        status: str = "Running",
        location: Optional[str] = "none",
        devices: Optional[Dict[str, Any]] = None,
        config: Optional[Dict[str, Any]] = None,
        expanded_devices: Optional[Dict[str, Any]] = None,
        expanded_config: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.name = name
        self.status = status
        self.location = location
        self.devices = devices or {}
        self.config = config or {}
        if expanded_devices is not None:
            self.expanded_devices = expanded_devices
        if expanded_config is not None:
            self.expanded_config = expanded_config
