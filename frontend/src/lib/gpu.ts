/**
 * One vocabulary and one set of numbers for GPUs, everywhere in the console.
 *
 * Two nouns, never swapped:
 *   GPU  = a compute GPU, sellable capacity        (gpu_total / gpu_used / gpu_available)
 *   card = a detected DRM card of any class        (gpu_cards_total / gpu_cards[])
 *
 * A Jetson Orin NX enumerates two cards for one physical GPU, so "1" and "2" are both true
 * about the same node and the label is what makes them readable. Wherever the compute ratio
 * appears, the cards count appears next to it whenever the two differ.
 *
 * LXD reports no GPU telemetry: there is no busy percentage and no VRAM usage anywhere in its
 * API. Everything here describes allocation, and the copy says so.
 */

import type { GPUCardInfo } from "@/types/api";
import { EMPTY_VALUE } from "@/components/ui/table-text";
import { plural } from "@/lib/utils";

export type GpuClass = "compute" | "integrated" | "display" | "unknown";

export const GPU_CLASS_LABEL: Record<GpuClass, string> = {
  compute: "Compute",
  integrated: "Integrated",
  display: "Display only",
  unknown: "Unclassified",
};

export const GPU_UTILIZATION_NOTE =
  "Allocation only. LXD reports which GPUs are attached to an instance, not how busy they are.";
export const GPU_EMPTY_TITLE = "No GPU detected";
// note: plain language since 2026-11-07. "No compute GPU" over "1 card detected" read as a
// contradiction to anyone who does not know that a server's onboard VGA is a card too.
export const GPU_NO_COMPUTE_TITLE = "No GPU for instances";
export const GPU_CAPACITY_NOTE =
  "A compute GPU is one an instance can be given, for example an NVIDIA, AMD or Intel discrete " +
  "card. Display cards, such as a server's onboard VGA, are detected but cannot be allocated.";
export const GPU_NOT_ALLOCATABLE = "Not allocatable";

/** What the backend classified this card as. The fallback never invents a compute GPU. */
export function gpuClassOf(card: GPUCardInfo): GpuClass {
  if (card.gpu_class && card.gpu_class !== "unknown") return card.gpu_class;
  if (card.is_compute) return "compute";
  if (card.is_compute === false) return "display";
  return "unknown";
}

/**
 * The card's name. Never runs through formatLabel: a driver ("nv_platform") and a product
 * name are registry-authored, and re-casing them is the x86_64 trap in another costume.
 */
export function gpuCardLabel(card: GPUCardInfo): string {
  const named = card.model_name || card.nvidia?.model || card.product;
  if (named) return named;

  const cls = gpuClassOf(card);
  if (cls === "display") return "Display controller";
  if (cls === "integrated") return card.vendor ? `${card.vendor} integrated graphics` : "Integrated graphics";
  const id = card.id ?? card.index;
  return `Card ${id}`;
}

/** The line under the name: what physically identifies this card. */
export function gpuCardQualifier(card: GPUCardInfo): string {
  const parts = [card.vendor, card.driver, card.pci_address || card.drm?.card_name].filter(Boolean);
  return parts.length > 0 ? parts.join(" · ") : EMPTY_VALUE;
}

/**
 * The selector that will actually be written into the LXD device.
 *
 * A Tegra iGPU has no PCI address, so `id` (the DRM card id) is the only way to name it. The
 * picker used to show both columns as dashes and zeros while silently choosing between them.
 */
export function gpuIdentifier(card: GPUCardInfo): {
  by: "pci" | "id";
  value: string;
  display: string;
} {
  if (card.pci_address) {
    return { by: "pci", value: card.pci_address, display: `PCI ${card.pci_address}` };
  }
  const id = card.id ?? card.drm?.id ?? card.index;
  return { by: "id", value: String(id), display: `ID ${id}` };
}

/** Compute first, then integrated, then display. Available before in use. */
export function sortGpuCards(cards: GPUCardInfo[]): GPUCardInfo[] {
  const rank: Record<GpuClass, number> = { compute: 0, integrated: 1, unknown: 2, display: 3 };
  return [...cards].sort((a, b) => {
    const byClass = rank[gpuClassOf(a)] - rank[gpuClassOf(b)];
    if (byClass !== 0) return byClass;
    if (a.in_use !== b.in_use) return a.in_use ? 1 : -1;
    return (a.id ?? a.index) - (b.id ?? b.index);
  });
}

export interface GpuSummarySource {
  gpu_total: number;
  gpu_used: number;
  gpu_available?: number;
  gpu_cards?: GPUCardInfo[];
  gpu_cards_total?: number;
  gpu_display_total?: number;
  gpu_integrated_total?: number;
}

export interface GpuSummary {
  computeTotal: number;
  used: number;
  available: number;
  cardsTotal: number;
  /** null when there is no compute GPU: a percentage of zero capacity is not zero percent. */
  allocationPercent: number | null;
  hasCards: boolean;
  hasCompute: boolean;
  extraCards: number;
  /** Every card is a display or integrated controller. False when the cards were not listed. */
  displayOnly: boolean;
}

export function summarizeGpus(src: GpuSummarySource | null | undefined): GpuSummary {
  const computeTotal = src?.gpu_total ?? 0;
  const used = Math.min(src?.gpu_used ?? 0, computeTotal);
  // Reads degrade rather than break while the backend rollout is in flight.
  const cardsTotal = src?.gpu_cards_total ?? src?.gpu_cards?.length ?? computeTotal;
  const cards = src?.gpu_cards ?? [];
  return {
    computeTotal,
    used,
    available: src?.gpu_available ?? Math.max(0, computeTotal - used),
    cardsTotal,
    allocationPercent: computeTotal > 0 ? (used / computeTotal) * 100 : null,
    hasCards: cardsTotal > 0,
    hasCompute: computeTotal > 0,
    extraCards: Math.max(0, cardsTotal - computeTotal),
    // From the cards when they were listed, otherwise from the per-class counts; a read with
    // neither cannot say "display" and falls back to "none allocatable".
    displayOnly:
      computeTotal === 0 &&
      cardsTotal > 0 &&
      (cards.length > 0
        ? cards.every((card) => {
            const cls = gpuClassOf(card);
            return cls === "display" || cls === "integrated";
          })
        : (src?.gpu_display_total ?? 0) + (src?.gpu_integrated_total ?? 0) >= cardsTotal),
  };
}

/** "0 / 1 compute GPUs allocated" */
export function gpuAllocationCaption(s: GpuSummary): string {
  return `${s.used} / ${plural(s.computeTotal, "compute GPU")} allocated`;
}

/** "2 cards detected", or null when the card count says nothing the GPU count did not. */
export function gpuCardsCaption(s: GpuSummary): string | null {
  if (s.extraCards <= 0) return null;
  return `${plural(s.cardsTotal, "card")} detected`;
}

/** "1 display card only", or "2 cards, none allocatable" when a card's class is not known. */
export function gpuNoComputeCaption(s: GpuSummary): string {
  return s.displayOnly
    ? `${plural(s.cardsTotal, "display card")} only`
    : `${plural(s.cardsTotal, "card")}, none allocatable`;
}

/** "0 / 1 GPUs", for a dense row that carries the cards count on the line below. */
export function gpuDenseRatio(s: GpuSummary): string {
  return `${s.used} / ${plural(s.computeTotal, "GPU")}`;
}

/** "1 compute GPU" */
export function gpuComputeCaption(s: GpuSummary): string {
  return plural(s.computeTotal, "compute GPU");
}
