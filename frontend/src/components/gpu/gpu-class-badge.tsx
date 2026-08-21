import { Badge } from "@/components/ui/badge";
import { GPU_CLASS_LABEL, gpuClassOf } from "@/lib/gpu";
import type { GPUCardInfo } from "@/types/api";

// Taxonomy, not status: filled for capacity, outline for attachable-but-not-capacity, muted
// for a card that cannot compute.
const VARIANT = {
  compute: "info",
  integrated: "outline",
  display: "muted",
  unknown: "muted",
} as const;

/**
 * Always rendered. An absent badge is indistinguishable from an unclassified card, which is
 * the ambiguity this whole surface exists to remove.
 */
export function GpuClassBadge({ card }: { card: GPUCardInfo }) {
  const cls = gpuClassOf(card);
  return <Badge variant={VARIANT[cls]}>{GPU_CLASS_LABEL[cls]}</Badge>;
}
