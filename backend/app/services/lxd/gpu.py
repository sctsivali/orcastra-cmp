"""
Canonical GPU inventory: the single source of every GPU number in the product.

LXD reports which cards exist and which instances hold them. It reports no GPU telemetry at
all, so this module answers "how many GPUs, and how many are allocated" and never invents a
utilization figure. Classification and card parsing live in gpu_model.py.

The functions here take duck-typed instance objects rather than pylxd models, so the whole
GPU model is testable without a cluster.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set

from app.models.schemas import GPUCardInfo

from .gpu_model import (
    GPU_COMPUTE_DRIVERS,
    GPU_DISPLAY_DRIVERS,
    GPU_INTEGRATED_DRIVERS,
    GpuClassification,
    _device_match_keys,
    card_match_keys,
    classify_gpu_card,
    driver_vendor_label,
    parse_gpu_cards,
)
from .utils import normalize_cluster_member

logger = logging.getLogger(__name__)

# The public GPU surface. Callers import everything GPU from here, so the classification
# details in gpu_model stay one module deep.
__all__ = [
    "GPU_COMPUTE_DRIVERS",
    "GPU_DISPLAY_DRIVERS",
    "GPU_INTEGRATED_DRIVERS",
    "GpuAllocations",
    "GpuClassification",
    "GpuInventory",
    "build_gpu_inventory",
    "card_match_keys",
    "classify_gpu_card",
    "driver_vendor_label",
    "parse_gpu_cards",
    "scan_gpu_allocations",
]


# Device nodes that unambiguously mean "this instance holds a compute GPU".
_NVIDIA_NODE_RE = re.compile(
    r"/dev/(nvidia\d*|nvidiactl|nvmap|nvgpu/|nvhost-[a-z\-]*gpu)", re.IGNORECASE
)
# A DRM node names one specific card. Whether it is an allocation depends on that card's
# class, so it is resolved against the card list rather than matched blindly.
_DRM_NODE_RE = re.compile(r"/dev/dri/(renderD\d+|card\d+)", re.IGNORECASE)


@dataclass
class GpuAllocations:
    usage_map: Dict[str, str] = field(default_factory=dict)   # card match key -> instance name
    compute_holders: Set[str] = field(default_factory=set)    # instances holding a compute card
    scan_failed: bool = False


def scan_gpu_allocations(
    instances: Optional[Iterable[Any]],
    compute_keys: Set[str],
    known_keys: Set[str],
    member: Optional[str] = None,
) -> GpuAllocations:
    """Walk the instance list once, producing both card attribution and the used count.

    One walk, so card attribution and the used count share one definition of "holds a GPU".
    """
    alloc = GpuAllocations()
    if instances is None:
        return alloc

    target = normalize_cluster_member(member)
    try:
        for instance in instances:
            if getattr(instance, "status", "") != "Running":
                continue

            # important: scope to this host. On a cluster `client.instances.all()` returns
            # every member's instances while capacity comes from ONE member, so an unscoped
            # count would report another node's GPU consumers against this node's cards.
            # A standalone host reports location "none", which normalizes to None and belongs
            # to whichever member is being asked about.
            location = normalize_cluster_member(getattr(instance, "location", None))
            if target and location and location != target:
                continue

            devices = getattr(instance, "expanded_devices", None) or getattr(instance, "devices", None) or {}
            config = getattr(instance, "expanded_config", None) or getattr(instance, "config", None) or {}
            name = getattr(instance, "name", "") or "unknown"

            # nvidia.runtime and nvidia.driver.capabilities bind the GPU into the container,
            # so each is an allocation on its own.
            # important: environment.LD_LIBRARY_PATH is NOT. An image can carry that variable
            # with no device attached, and treating it as an allocation would consume the
            # host's only GPU on a container that never touched it.
            holds_compute = (
                str(config.get("nvidia.runtime", "")).strip().lower() in ("true", "1")
                or bool(config.get("nvidia.driver.capabilities"))
            )

            for device in (devices or {}).values():
                if not isinstance(device, dict):
                    continue

                if device.get("type") == "gpu":
                    hits = _device_match_keys(device)
                    for key in hits:
                        alloc.usage_map.setdefault(key, name)
                    # an unqualified `type: gpu` device selects every GPU on the host
                    holds_compute = holds_compute or not hits or bool(hits & compute_keys)
                    continue

                reference = f"{device.get('source', '')} {device.get('path', '')}"
                if _NVIDIA_NODE_RE.search(reference):
                    holds_compute = True
                    continue

                match = _DRM_NODE_RE.search(reference)
                if match:
                    node = match.group(1).lower()
                    key = f"{'render' if node.startswith('renderd') else 'card'}:{node}"
                    if key in known_keys:
                        alloc.usage_map.setdefault(key, name)
                        # important: a /dev/dri/renderD* passthrough is a VAAPI transcode as
                        # often as it is CUDA. It marks the card it names in use, but it only
                        # consumes capacity when that card is a compute card. On a Jetson
                        # renderD128 is the display controller and renderD129 is the GPU.
                        if key in compute_keys:
                            holds_compute = True

            if holds_compute:
                alloc.compute_holders.add(name)
    except Exception as e:
        # A failed scan must not read as "nothing is allocated": usage counts only the instances
        # walked before the failure, so scan_failed flags it. Capacity stays intact.
        logger.debug(f"GPU allocation scan failed: {e}")
        alloc.scan_failed = True
    return alloc


@dataclass
class GpuInventory:
    """Every GPU number for one host, derived once."""

    cards: List[GPUCardInfo] = field(default_factory=list)
    gpu_total: int = 0                 # compute cards: sellable capacity
    gpu_integrated_total: int = 0      # attachable integrated graphics, not capacity
    gpu_display_total: int = 0
    gpu_unknown_total: int = 0
    gpu_cards_total: int = 0           # every DRM/GPU card LXD reported
    gpu_used: int = 0                  # clamped to gpu_total
    gpu_available: int = 0             # never negative
    vendors: List[str] = field(default_factory=list)
    models: List[str] = field(default_factory=list)
    # LXD exposes no GPU telemetry. This stays False until a metrics agent provides real
    # utilization, and the UI uses it to keep saying "allocation" rather than "usage".
    utilization_supported: bool = False

    @classmethod
    def empty(cls) -> "GpuInventory":
        return cls()

    def merge(self, other: "GpuInventory") -> "GpuInventory":
        """Fold a per-member inventory into a cluster-wide one."""
        merged = GpuInventory(
            cards=self.cards + other.cards,
            gpu_total=self.gpu_total + other.gpu_total,
            gpu_integrated_total=self.gpu_integrated_total + other.gpu_integrated_total,
            gpu_display_total=self.gpu_display_total + other.gpu_display_total,
            gpu_unknown_total=self.gpu_unknown_total + other.gpu_unknown_total,
            gpu_cards_total=self.gpu_cards_total + other.gpu_cards_total,
            gpu_used=self.gpu_used + other.gpu_used,
            vendors=sorted(set(self.vendors) | set(other.vendors)),
            models=sorted(set(self.models) | set(other.models)),
        )
        merged.gpu_available = max(0, merged.gpu_total - merged.gpu_used)
        return merged


def build_gpu_inventory(
    resources_metadata: Optional[Dict[str, Any]],
    instances: Optional[Iterable[Any]] = None,
    member: Optional[str] = None,
) -> GpuInventory:
    """The single source of every GPU number in the product.

    `resources_metadata` is a raw LXD /1.0/resources body (client.resources, or the
    ?target=<member> metadata). `member` scopes instance enumeration by instance.location.
    """
    metadata = resources_metadata or {}
    gpu_block = metadata.get("gpu") or {}
    raw_cards = gpu_block.get("cards") or []
    system = metadata.get("system") or {}
    memory_total = (metadata.get("memory") or {}).get("total")

    if not raw_cards:
        return GpuInventory.empty()

    classifications = [classify_gpu_card(card) for card in raw_cards]
    key_sets = [card_match_keys(card) for card in raw_cards]
    compute_keys = {
        key
        for classification, keys in zip(classifications, key_sets)
        if classification.is_compute
        for key in keys
    }
    known_keys = {key for keys in key_sets for key in keys}

    allocations = scan_gpu_allocations(instances, compute_keys, known_keys, member)
    cards = parse_gpu_cards(
        raw_cards,
        usage_map=allocations.usage_map,
        system_memory_total=memory_total,
        system_product=system.get("product"),
        system_vendor=system.get("vendor"),
    )

    gpu_total = sum(1 for card in cards if card.is_compute)
    gpu_used = min(gpu_total, len(allocations.compute_holders))

    return GpuInventory(
        cards=cards,
        gpu_total=gpu_total,
        gpu_integrated_total=sum(1 for card in cards if card.gpu_class == "integrated"),
        gpu_display_total=sum(1 for card in cards if card.gpu_class == "display"),
        gpu_unknown_total=sum(1 for card in cards if card.gpu_class == "unknown"),
        gpu_cards_total=len(raw_cards),
        gpu_used=gpu_used,
        gpu_available=max(0, gpu_total - gpu_used),
        vendors=sorted({card.vendor for card in cards if card.is_compute and card.vendor}),
        models=sorted({card.model_name for card in cards if card.is_compute and card.model_name}),
    )
